# -*- test-case-name: txdav.base.datastore.test.test_subpostgres -*-
# #
# Copyright (c) 2010-2017 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# #

"""
Run and manage PostgreSQL as a subprocess.
"""

import os
import pwd
import re
import signal
from hashlib import md5
from pipes import quote as shell_quote


from twisted.python.procutils import which
from twisted.internet.protocol import ProcessProtocol

from twext.enterprise.dal.parseschema import splitSQLString
from twext.python.log import Logger
from twext.python.filepath import CachingFilePath

from twisted.protocols.basic import LineReceiver
from twisted.internet.defer import Deferred, succeed
from txdav.base.datastore.dbapiclient import DBAPIConnector
from txdav.base.datastore.dbapiclient import postgres
from txdav.common.icommondatastore import InternalDataStoreError

from twisted.application.service import MultiService

log = Logger()

# This appears in the postgres log to indicate that it is accepting
# connections.
_MAGIC_READY_COOKIE = "database system is ready to accept connections"


class PostgresMonitor(ProcessProtocol):
    """
    A monitoring protocol which watches the postgres subprocess.
    """
    log = Logger()

    def __init__(self, svc=None):
        self.lineReceiver = LineReceiver()
        self.lineReceiver.delimiter = '\n'
        self.lineReceiver.lineReceived = self.lineReceived
        self.svc = svc
        self.isReady = False
        self.completionDeferred = Deferred()

    def lineReceived(self, line):
        if self.svc is None:
            return
        if not self.isReady:
            if _MAGIC_READY_COOKIE in line:
                self.svc.ready()

    disconnecting = False

    def connectionMade(self):
        self.lineReceiver.makeConnection(self)

    def outReceived(self, out):
        for line in out.split("\n"):
            if line:
                self.log.info("{msg}", msg=line)
        # self.lineReceiver.dataReceived(out)

    def errReceived(self, err):
        for line in err.split("\n"):
            if line:
                self.log.error("{msg}", msg=line)
        self.lineReceiver.dataReceived(err)

    def processEnded(self, reason):
        self.log.info(
            "pg_ctl process ended with status={status}",
            status=reason.value.status
        )
        # If pg_ctl exited with zero, we were successful in starting postgres
        # If pg_ctl exited with nonzero, we need to give up.
        self.lineReceiver.connectionLost(reason)

        if reason.value.status == 0:
            self.completionDeferred.callback(None)
        else:
            self.log.error("Could not start postgres; see postgres.log")
            self.completionDeferred.errback(reason)


class ErrorOutput(Exception):
    """
    The process produced some error output and exited with a non-zero exit
    code.
    """


class CapturingProcessProtocol(ProcessProtocol):
    """
    A L{ProcessProtocol} that captures its output and error.

    @ivar output: a C{list} of all C{str}s received to stderr.

    @ivar error: a C{list} of all C{str}s received to stderr.
    """

    def __init__(self, deferred, inputData):
        """
        Initialize a L{CapturingProcessProtocol}.

        @param deferred: the L{Deferred} to fire when the process is complete.

        @param inputData: a C{str} to feed to the subprocess's stdin.
        """
        self.deferred = deferred
        self.input = inputData
        self.output = []
        self.error = []

    def connectionMade(self):
        """
        The process started; feed its input on stdin.
        """
        if self.input is not None:
            self.transport.write(self.input)
            self.transport.closeStdin()

    def outReceived(self, data):
        """
        Some output was received on stdout.
        """
        self.output.append(data)

    def errReceived(self, data):
        """
        Some output was received on stderr.
        """
        self.output.append(data)

    def processEnded(self, why):
        """
        The process is over, fire the Deferred with the output.
        """
        self.deferred.callback("".join(self.output))


class PostgresService(MultiService):

    def __init__(
        self, dataStoreDirectory, subServiceFactory,
        schema, resetSchema=False, databaseName="subpostgres",
        clusterName="cluster",
        logFile="postgres.log",
        logDirectory="",
        socketDir="",
        socketName="",
        listenAddresses=[],
        txnTimeoutSeconds=30,
        sharedBuffers=30,
        maxConnections=20,
        options=[],
        testMode=False,
        uid=None, gid=None,
        spawnedDBUser="caldav",
        pgCtl="pg_ctl",
        initDB="initdb",
        reactor=None,
    ):
        """
        Initialize a L{PostgresService} pointed at a data store directory.

        @param dataStoreDirectory: the directory to
        @type dataStoreDirectory: L{twext.python.filepath.CachingFilePath}

        @param subServiceFactory: a 1-arg callable that will be called with a
            1-arg callable which returns a DB-API cursor.
        @type subServiceFactory: C{callable}

        @param spawnedDBUser: the postgres role
        @type spawnedDBUser: C{str}
        """

        # FIXME: By default there is very little (4MB) shared memory available,
        # so at the moment I am lowering these postgres config options to allow
        # multiple servers to run.  We might want to look into raising
        # kern.sysv.shmmax.
        # See: http://www.postgresql.org/docs/8.4/static/kernel-resources.html

        MultiService.__init__(self)
        self.subServiceFactory = subServiceFactory
        self.dataStoreDirectory = dataStoreDirectory
        self.workingDir = self.dataStoreDirectory.child("working")
        self.resetSchema = resetSchema

        # In order to delay a shutdown until database initialization has
        # completed, our stopService( ) examines the delayedShutdown flag.
        # If True, we wait on the shutdownDeferred to fire before proceeding.
        # The deferred gets fired once database init is complete.
        self.delayedShutdown = False  # set to True when in critical code
        self.shutdownDeferred = None  # the actual deferred

        # Options from config
        self.databaseName = databaseName
        self.clusterName = clusterName
        # Make logFile absolute in case the working directory of postgres is
        # elsewhere:
        self.logFile = os.path.abspath(logFile)
        if logDirectory:
            self.logDirectory = os.path.abspath(logDirectory)
        else:
            self.logDirectory = ""

        # Always use our own configured socket dir in case the built-in
        # postgres tries to use a directory we don't have permissions for
        if not socketDir:
            # Socket directory was not specified, so come up with one
            # in /tmp and based on a hash of the data store directory
            digest = md5(dataStoreDirectory.path).hexdigest()
            socketDir = "/tmp/ccs_postgres_" + digest
        self.socketDir = CachingFilePath(socketDir)
        self.socketName = socketName

        if listenAddresses:
            if ":" in listenAddresses[0]:
                self.host, self.port = listenAddresses[0].split(":")
            else:
                self.host, self.port = (listenAddresses[0], None)

            self.listenAddresses = [
                addr.split(":")[0] for addr in listenAddresses
            ]
        else:
            self.host = self.socketDir.path
            self.port = None
            self.listenAddresses = []

        self.txnTimeoutSeconds = txnTimeoutSeconds

        self.testMode = testMode
        self.sharedBuffers = max(sharedBuffers if not testMode else 16, 16)
        self.maxConnections = maxConnections if not testMode else 8
        self.options = options

        self.uid = uid
        self.gid = gid
        self.spawnedDBUser = spawnedDBUser
        self.schema = schema
        self.monitor = None
        self.openConnections = []

        def locateCommand(name, cmd):
            for found in which(cmd):
                return found

            raise InternalDataStoreError(
                "Unable to locate {} command: {}".format(name, cmd)
            )

        self._pgCtl = locateCommand("pg_ctl", pgCtl)

        # Make note of the inode for the pg_ctl script; if it changes or is
        # missing when it comes time to stop postgres, instead send SIGTERM
        # to stop our postgres (since we can't do a graceful shutdown)
        try:
            self._pgCtlInode = os.stat(self._pgCtl).st_ino
        except:
            self._pgCtlInode = 0

        self._initdb = locateCommand("initdb", initDB)
        self._reactor = reactor
        self._postgresPid = None

    @property
    def reactor(self):
        if self._reactor is None:
            from twisted.internet import reactor
            self._reactor = reactor
        return self._reactor

    def activateDelayedShutdown(self):
        """
        Call this when starting database initialization code to
        protect against shutdown.

        Sets the delayedShutdown flag to True so that if reactor shutdown
        commences, the shutdown will be delayed until deactivateDelayedShutdown
        is called.
        """
        self.delayedShutdown = True

    def deactivateDelayedShutdown(self):
        """
        Call this when database initialization code has completed so that the
        reactor can shutdown.
        """
        self.delayedShutdown = False
        if self.shutdownDeferred:
            self.shutdownDeferred.callback(None)

    def _connectorFor(self, databaseName=None):
        if databaseName is None:
            databaseName = self.databaseName

        kwargs = {
            "database": databaseName,
        }

        if self.host.startswith("/"):
            kwargs["endpoint"] = "unix:{}".format(self.host)
        else:
            kwargs["endpoint"] = "tcp:{}".format(self.host)
            if self.port:
                kwargs["endpoint"] = "{}:{}".format(kwargs["endpoint"], self.port)
        if self.spawnedDBUser:
            kwargs["user"] = self.spawnedDBUser
        elif self.uid is not None:
            kwargs["user"] = pwd.getpwuid(self.uid).pw_name
        kwargs["txnTimeoutSeconds"] = self.txnTimeoutSeconds

        return DBAPIConnector.connectorFor("postgres", **kwargs)

    def produceConnection(self, label="<unlabeled>", databaseName=None):
        """
        Produce a DB-API 2.0 connection pointed at this database.
        """
        return self._connectorFor(databaseName).connect(label)

    def ready(self, createDatabaseConn, createDatabaseCursor):
        """
        Subprocess is ready.  Time to initialize the subservice.
        If the database has not been created and there is a dump file,
        then the dump file is imported.
        """
        if self.resetSchema:
            try:
                createDatabaseCursor.execute(
                    "drop database {}".format(self.databaseName)
                )
            except postgres.DatabaseError:
                pass

        try:
            createDatabaseCursor.execute(
                "create database {} with encoding 'UTF8'"
                .format(self.databaseName)
            )
        except:
            # database already exists
            sqlToExecute = None
        else:
            # database does not yet exist; if dump file exists, execute it,
            # otherwise execute schema
            sqlToExecute = self.schema

        createDatabaseCursor.close()
        createDatabaseConn.close()

        if sqlToExecute is not None:
            connection = self.produceConnection()
            cursor = connection.cursor()
            for statement in splitSQLString(sqlToExecute):
                cursor.execute(statement)
            connection.commit()
            connection.close()

        if self.shutdownDeferred is None:
            # Only continue startup if we've not begun shutdown
            self.subServiceFactory(
                self.produceConnection, self
            ).setServiceParent(self)

    def pauseMonitor(self):
        """
        Pause monitoring.  This is a testing hook for when (if) we are
        continuously monitoring output from the 'postgres' process.
        """
#        for pipe in self.monitor.transport.pipes.values():
#            pipe.stopReading()
#            pipe.stopWriting()
        pass

    def unpauseMonitor(self):
        """
        Unpause monitoring.

        @see: L{pauseMonitor}
        """
#        for pipe in self.monitor.transport.pipes.values():
#            pipe.startReading()
#            pipe.startWriting()
        pass

    def startDatabase(self):
        """
        Start the database and initialize the subservice.
        """
        def createConnection():
            try:
                createDatabaseConn = self.produceConnection(
                    "schema creation", "postgres"
                )
            except postgres.DatabaseError as e:
                log.error(
                    "Unable to connect to database for schema creation:"
                    " {error}",
                    error=e
                )
                raise

            createDatabaseCursor = createDatabaseConn.cursor()

            if postgres.__name__ == "pg8000":
                createDatabaseConn.realConnection.autocommit = True
            elif postgres.__name__ == "pgdb":
                createDatabaseCursor.execute("commit")
            else:
                raise InternalDataStoreError(
                    "Unknown Postgres DBM module: {}".format(postgres)
                )

            return createDatabaseConn, createDatabaseCursor

        monitor = PostgresMonitor(self)
        # check consistency of initdb and postgres?

        options = []
        options.append(
            "-c listen_addresses={}"
            .format(shell_quote(",".join(self.listenAddresses)))
        )
        if self.socketDir:
            options.append(
                "-c unix_socket_directories={}"
                .format(shell_quote(self.socketDir.path))
            )
        if self.port:
            options.append(
                "-c port={}".format(shell_quote(self.port))
            )
        options.append(
            "-c shared_buffers={:d}"
            .format(self.sharedBuffers)  # int: don't quote
        )
        options.append(
            "-c max_connections={:d}"
            .format(self.maxConnections)  # int: don't quote
        )
        options.append("-c standard_conforming_strings=on")
        options.append("-c unix_socket_permissions=0770")
        options.extend(self.options)
        if self.logDirectory:  # tell postgres to rotate logs
            options.append(
                "-c log_directory={}".format(shell_quote(self.logDirectory))
            )
            options.append("-c log_truncate_on_rotation=on")
            options.append("-c log_filename=postgresql_%w.log")
            options.append("-c log_rotation_age=1440")
            options.append("-c logging_collector=on")

        options.append("-c log_line_prefix=%t")
        if self.testMode:
            options.append("-c log_statement=all")

        args = [
            self._pgCtl, "start",
            "--log={}".format(self.logFile),
            "--timeout=86400",  # Plenty of time for a long cluster upgrade
            "-w",  # Wait for startup to complete
            "-o", " ".join(options),  # Options passed to postgres
        ]

        log.info("Requesting postgres start via: {args}", args=args)
        self.reactor.spawnProcess(
            monitor, self._pgCtl, args,
            env=self.env, path=self.workingDir.path,
            uid=self.uid, gid=self.gid,
        )
        self.monitor = monitor

        def gotStatus(result):
            """
            Grab the postgres pid from the pgCtl status call in case we need
            to kill it directly later on in hardStop().  Useful in conjunction
            with the DataStoreMonitor so we can shut down if DataRoot has been
            removed/renamed/unmounted.
            """
            reResult = re.search("PID: (\d+)\D", result)
            if reResult is not None:
                self._postgresPid = int(reResult.group(1))
            self.ready(*createConnection())
            self.deactivateDelayedShutdown()

        def gotReady(result):
            """
            We started postgres; we're responsible for stopping it later.
            Call pgCtl status to get the pid.
            """
            log.info("{cmd} exited", cmd=self._pgCtl)
            self.shouldStopDatabase = True
            d = Deferred()
            statusMonitor = CapturingProcessProtocol(d, None)
            self.reactor.spawnProcess(
                statusMonitor, self._pgCtl, [self._pgCtl, "status"],
                env=self.env, path=self.workingDir.path,
                uid=self.uid, gid=self.gid,
            )
            return d.addCallback(gotStatus)

        def couldNotStart(f):
            """
            There was an error trying to start postgres.  Try to connect
            because it might already be running.  In this case, we won't
            be the one to stop it.
            """
            d = Deferred()
            statusMonitor = CapturingProcessProtocol(d, None)
            self.reactor.spawnProcess(
                statusMonitor, self._pgCtl, [self._pgCtl, "status"],
                env=self.env, path=self.workingDir.path,
                uid=self.uid, gid=self.gid,
            )
            return d.addCallback(gotStatus).addErrback(giveUp)

        def giveUp(f):
            """
            We can't start postgres or connect to a running instance.  Shut
            down.
            """
            log.critical(
                "Can't start or connect to postgres: {failure.value}",
                failure=f
            )
            self.deactivateDelayedShutdown()
            self.reactor.stop()

        self.monitor.completionDeferred.addCallback(
            gotReady).addErrback(couldNotStart)

    shouldStopDatabase = False

    def startService(self):
        MultiService.startService(self)
        self.activateDelayedShutdown()
        clusterDir = self.dataStoreDirectory.child(self.clusterName)
        env = self.env = os.environ.copy()
        env.update(PGDATA=clusterDir.path,
                   PGHOST=self.host,
                   PGUSER=self.spawnedDBUser)

        if self.socketDir:
            if not self.socketDir.isdir():
                log.info("Creating {dir}", dir=self.socketDir.path.decode("utf-8"))
                self.socketDir.createDirectory()

            if self.uid and self.gid:
                os.chown(self.socketDir.path, self.uid, self.gid)

            os.chmod(self.socketDir.path, 0o770)

        if not self.dataStoreDirectory.isdir():
            log.info("Creating {dir}", dir=self.dataStoreDirectory.path.decode("utf-8"))
            self.dataStoreDirectory.createDirectory()

        if not self.workingDir.isdir():
            log.info("Creating {dir}", dir=self.workingDir.path.decode("utf-8"))
            self.workingDir.createDirectory()

        if self.uid and self.gid:
            os.chown(self.dataStoreDirectory.path, self.uid, self.gid)
            os.chown(self.workingDir.path, self.uid, self.gid)

        if not clusterDir.isdir():
            # No cluster directory, run initdb
            log.info("Running initdb for {dir}", dir=clusterDir.path.decode("utf-8"))
            dbInited = Deferred()
            self.reactor.spawnProcess(
                CapturingProcessProtocol(dbInited, None),
                self._initdb,
                [self._initdb, "-E", "UTF8", "-U", self.spawnedDBUser],
                env=env, path=self.workingDir.path,
                uid=self.uid, gid=self.gid,
            )

            def doCreate(result):
                if result.find("FATAL:") != -1:
                    log.error(result)
                    raise InternalDataStoreError(
                        "Unable to initialize postgres database: {}"
                        .format(result)
                    )
                self.startDatabase()

            dbInited.addCallback(doCreate)

        else:
            log.info("Cluster already exists at {dir}", dir=clusterDir.path.decode("utf-8"))
            self.startDatabase()

    def stopService(self):
        """
        Stop all child services, then stop the subprocess, if it's running.
        """

        if self.delayedShutdown:
            # We're still in the process of initializing the database, so
            # delay shutdown until the shutdownDeferred fires.
            d = self.shutdownDeferred = Deferred()
            d.addCallback(lambda ignored: MultiService.stopService(self))
        else:
            d = MultiService.stopService(self)

        def superStopped(result):
            # If pg_ctl's startup wasn't successful, don't bother to stop the
            # database.  (This also happens in command-line tools.)
            if self.shouldStopDatabase:

                # Compare pg_ctl inode with one we saw at the start; if different
                # (or missing), fall back to SIGTERM
                try:
                    newInode = os.stat(self._pgCtl).st_ino
                except OSError:
                    # Missing
                    newInode = -1

                if self._pgCtlInode != newInode:
                    # send SIGTERM to postgres
                    log.info("Postgres control script mismatch")
                    if self._postgresPid:
                        log.info("Sending SIGTERM to Postgres")
                        try:
                            os.kill(self._postgresPid, signal.SIGTERM)
                        except OSError:
                            pass
                    return succeed(None)
                else:
                    # use pg_ctl stop
                    monitor = PostgresMonitor()
                    args = [
                        self._pgCtl, "stop",
                        "--log={}".format(self.logFile),
                    ]
                    log.info("Requesting postgres stop via: {args}", args=args)
                    self.reactor.spawnProcess(
                        monitor, self._pgCtl,
                        args,
                        env=self.env, path=self.workingDir.path,
                        uid=self.uid, gid=self.gid,
                    )
                    return monitor.completionDeferred
        return d.addCallback(superStopped)

    def hardStop(self):
        """
        Stop postgres quickly by sending it SIGQUIT
        """
        if self._postgresPid is not None:
            try:
                os.kill(self._postgresPid, signal.SIGQUIT)
            except OSError:
                pass
