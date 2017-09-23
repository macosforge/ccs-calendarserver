##
# Copyright (c) 2005-2017 Apple Inc. All rights reserved.
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
##
from twisted.python import failure

"""
Inbound IMIP mail handling for Calendar Server
"""

from twext.enterprise.dal.record import fromTable
from twext.enterprise.jobs.workitem import WorkItem, RegeneratingWorkItem
from twext.internet.gaiendpoint import GAIEndpoint
from twext.internet.ssl import simpleClientContextFactory
from twext.python.log import Logger

from twisted.application import service
from twisted.internet import protocol, defer
from twisted.internet.defer import inlineCallbacks, returnValue, succeed
from twisted.mail import pop3client, imap4
from twisted.python.log import LogPublisher

from twistedcaldav.config import config
from twistedcaldav.ical import Property, Component

from txdav.caldav.datastore.scheduling.imip.scheduler import IMIPScheduler
from txdav.caldav.datastore.scheduling.imip.smtpsender import SMTPSender
from txdav.caldav.datastore.scheduling.itip import iTIPRequestStatus
from txdav.common.datastore.sql_tables import schema

import datetime
import dateutil.parser
import dateutil.tz
import email.utils

from OpenSSL.SSL import Error as TLSError
from calendarserver.tap.util import AlertPoster

log = Logger()

#
# Monkey patch imap4.log so it doesn't emit useless logging,
# specifically, "Unhandled unsolicited response" nonsense.
#


class IMAPLogger(LogPublisher):

    def msg(self, *message, **kwargs):
        if message and message[0].startswith("Unhandled unsolicited response:"):
            return

        LogPublisher.msg(self, *message, **kwargs)

    def err(self, _stuff=None, _why=None, **kw):
        """
        Write a failure to the log.

        The C{_stuff} and C{_why} parameters use an underscore prefix to lessen
        the chance of colliding with a keyword argument the application wishes
        to pass.  It is intended that they be supplied with arguments passed
        positionally, not by keyword.

        @param _stuff: The failure to log.  If C{_stuff} is L{None} a new
            L{Failure} will be created from the current exception state.  If
            C{_stuff} is an C{Exception} instance it will be wrapped in a
            L{Failure}.
        @type _stuff: L{None}, C{Exception}, or L{Failure}.

        @param _why: The source of this failure.  This will be logged along with
            C{_stuff} and should describe the context in which the failure
            occurred.
        @type _why: C{str}
        """
        if _stuff is None:
            _stuff = failure.Failure()
        if isinstance(_stuff, failure.Failure):
            self.msg(failure=_stuff, why=_why, isError=1, **kw)
        elif isinstance(_stuff, Exception):
            self.msg(failure=failure.Failure(_stuff), why=_why, isError=1, **kw)
        else:
            self.msg(repr(_stuff), why=_why, isError=1, **kw)

imap4.log = IMAPLogger()


""" SCHEMA:
create table IMIP_REPLY_WORK (
  WORK_ID                       integer primary key default nextval('WORKITEM_SEQ') not null,
  NOT_BEFORE                    timestamp    default timezone('UTC', CURRENT_TIMESTAMP),
  ORGANIZER                     varchar(255) not null,
  ATTENDEE                      varchar(255) not null,
  ICALENDAR_TEXT                text         not null
);
create table IMIP_POLLING_WORK (
  WORK_ID                       integer primary key default nextval('WORKITEM_SEQ') not null,
  NOT_BEFORE                    timestamp    default timezone('UTC', CURRENT_TIMESTAMP)
);
"""


class IMIPReplyWork(WorkItem, fromTable(schema.IMIP_REPLY_WORK)):

    @inlineCallbacks
    def doWork(self):
        calendar = Component.fromString(self.icalendarText)
        try:
            yield injectMessage(self.transaction, self.organizer, self.attendee, calendar)
        except:
            log.error("Unable to process reply")


class IMIPPollingWork(RegeneratingWorkItem, fromTable(schema.IMIP_POLLING_WORK)):

    # FIXME: purge all old tokens here
    group = "imip_polling"

    def regenerateInterval(self):
        """
        Return the interval in seconds between regenerating instances.
        """
        mailRetriever = self.transaction._mailRetriever
        if mailRetriever is not None:
            return mailRetriever.settings["PollingSeconds"]

        # The lack of mailRetriever means IMIP polling is turned off.
        # Returning None will cause this work item to no longer be scheduled.
        return None

    @inlineCallbacks
    def doWork(self):

        mailRetriever = self.transaction._mailRetriever
        if mailRetriever is not None:
            yield mailRetriever.fetchMail()


class MailRetriever(service.Service):

    def __init__(self, store, directory, settings, reactor=None):
        self.store = store
        self.settings = settings
        if reactor is None:
            from twisted.internet import reactor
        self.reactor = reactor

        # If we're using our dedicated account on our local server, we're free
        # to delete all messages that arrive in the inbox so as to not let
        # cruft build up
        self.deleteAllMail = shouldDeleteAllMail(
            config.ServerHostName,
            settings.Server, settings.Username)
        self.mailReceiver = MailReceiver(store, directory)
        mailType = settings['Type']
        if mailType.lower().startswith('pop'):
            self.factory = POP3DownloadFactory
        else:
            self.factory = IMAP4DownloadFactory

        contextFactory = None
        if settings["UseSSL"]:
            contextFactory = simpleClientContextFactory(settings.Server)
        self.point = GAIEndpoint(
            self.reactor, settings.Server,
            settings.Port, contextFactory=contextFactory)

    def fetchMail(self):
        return self.point.connect(self.factory(
            self.settings, self.mailReceiver,
            self.deleteAllMail))

    @inlineCallbacks
    def scheduleNextPoll(self, seconds=None):
        if seconds is None:
            seconds = self.settings["PollingSeconds"]
        yield IMIPPollingWork.reschedule(self.store, seconds)


def shouldDeleteAllMail(serverHostName, inboundServer, username):
    """
    Given the hostname of the calendar server, the hostname of the pop/imap
    server, and the username we're using to access inbound mail, determine
    whether we should delete all messages in the inbox or whether to leave
    all unprocessed messages.

    @param serverHostName: the calendar server hostname (config.ServerHostName)
    @type serverHostName: C{str}
    @param inboundServer: the pop/imap server hostname
    @type inboundServer: C{str}
    @param username: the name of the account we're using to retrieve mail
    @type username: C{str}
    @return: True if we should delete all messages from the inbox, False otherwise
    @rtype: C{boolean}
    """
    return (
        inboundServer in (serverHostName, "localhost") and
        username == "com.apple.calendarserver"
    )


@inlineCallbacks
def scheduleNextMailPoll(store, seconds):
    txn = store.newTransaction(label="scheduleNextMailPoll")
    notBefore = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    log.debug("Scheduling next mail poll: {time}", time=notBefore)
    yield txn.enqueue(IMIPPollingWork, notBefore=notBefore)
    yield txn.commit()


def sanitizeCalendar(calendar):
    """
    Clean up specific issues seen in the wild from third party IMIP capable
    servers.

    @param calendar: the calendar Component to sanitize
    @type calendar: L{Component}
    """
    # Don't let a missing PRODID prevent the reply from being processed
    if not calendar.hasProperty("PRODID"):
        calendar.addProperty(
            Property(
                "PRODID", "Unknown"
            )
        )

    # For METHOD:REPLY we can remove STATUS properties
    methodProperty = calendar.getProperty("METHOD")
    if methodProperty is not None:
        # Uppercase any METHOD properties
        methodProperty.setValue(methodProperty.value().upper())
        if methodProperty.value() == "REPLY":
            calendar.removeAllPropertiesWithName("STATUS")


class MailReceiver(object):

    NO_TOKEN = 0
    UNKNOWN_TOKEN = 1
    UNKNOWN_TOKEN_OLD = 2
    MALFORMED_TO_ADDRESS = 3
    NO_ORGANIZER_ADDRESS = 4
    REPLY_FORWARDED_TO_ORGANIZER = 5
    INJECTION_SUBMITTED = 6
    INCOMPLETE_DSN = 7
    UNKNOWN_FAILURE = 8

    def __init__(self, store, directory):
        self.store = store
        self.directory = directory

    def checkDSN(self, message):
        # returns (isdsn, action, icalendar attachment)

        report = deliveryStatus = calBody = None

        for part in message.walk():
            contentType = part.get_content_type()
            if contentType == "multipart/report":
                report = part
                continue
            elif contentType == "message/delivery-status":
                deliveryStatus = part
                continue
            elif contentType == "message/rfc822":
                # original = part
                continue
            elif contentType == "text/calendar":
                calBody = part.get_payload(decode=True)
                continue

        if report is not None and deliveryStatus is not None:
            # we have what appears to be a dsn

            lines = str(deliveryStatus).split("\n")
            for line in lines:
                lower = line.lower()
                if lower.startswith("action:"):
                    # found action:
                    action = lower.split(' ')[1]
                    break
            else:
                action = None

            return True, action, calBody

        else:
            # not a dsn
            return False, None, None

    def _extractToken(self, text):
        try:
            pre, _ignore_post = text.split('@')
            pre, token = pre.split('+')
            return token
        except ValueError:
            return None

    @inlineCallbacks
    def processDSN(self, calBody, msgId):
        calendar = Component.fromString(calBody)
        # Extract the token (from organizer property)
        organizer = calendar.getOrganizer()
        token = self._extractToken(organizer)
        if not token:
            log.error("Mail gateway can't find token in DSN {msgid}", msgid=msgId)
            return

        txn = self.store.newTransaction(label="MailReceiver.processDSN")
        records = (yield txn.imipLookupByToken(token))
        yield txn.commit()
        try:
            # Note the results are returned as utf-8 encoded strings
            record = records[0]
        except:
            # This isn't a token we recognize
            log.error(
                "Mail gateway found a token ({token}) but didn't recognize it in message {msgid}",
                token=token, msgid=msgId,
            )
            returnValue(self.UNKNOWN_TOKEN)

        calendar.removeAllButOneAttendee(record.attendee)
        calendar.getOrganizerProperty().setValue(organizer)
        for comp in calendar.subcomponents():
            if comp.name() == "VEVENT":
                comp.addProperty(Property(
                    "REQUEST-STATUS",
                    ["5.1", "Service unavailable"]))
                break
        else:
            # no VEVENT in the calendar body.
            # TODO: what to do in this case?
            pass

        log.warn("Mail gateway processing DSN {msgid}", msgid=msgId)
        txn = self.store.newTransaction(label="MailReceiver.processDSN")
        yield txn.enqueue(
            IMIPReplyWork,
            organizer=record.organizer,
            attendee=record.attendee,
            icalendarText=str(calendar)
        )
        yield txn.commit()
        returnValue(self.INJECTION_SUBMITTED)

    @inlineCallbacks
    def processReply(self, msg):
        # extract the token from the To header
        _ignore_name, addr = email.utils.parseaddr(msg['To'])
        if addr:
            # addr looks like: server_address+token@example.com
            token = self._extractToken(addr)
            if not token:
                log.error(
                    "Mail gateway didn't find a token in message "
                    "{msgid} ({to})", msgid=msg['Message-ID'], to=msg['To']
                )
                returnValue(self.NO_TOKEN)
        else:
            log.error(
                "Mail gateway couldn't parse To: address ({to}) in "
                "message {msgid}", to=msg['To'], msgid=msg['Message-ID']
            )
            returnValue(self.MALFORMED_TO_ADDRESS)

        txn = self.store.newTransaction(label="MailReceiver.processReply")
        records = (yield txn.imipLookupByToken(token))
        yield txn.commit()
        try:
            # Note the results are returned as utf-8 encoded strings
            record = records[0]
        except:
            # This isn't a token we recognize
            log.info(
                "Mail gateway found a token ({token}) but didn't "
                "recognize it in message {msgid}",
                token=token, msgid=msg['Message-ID']
            )
            # Any email with an unknown token which was sent over 72 hours ago
            # is deleted.  If we can't parse the date we leave it in the inbox.
            dateString = msg.get("Date")
            if dateString is not None:
                try:
                    dateSent = dateutil.parser.parse(dateString)
                except Exception as e:
                    log.info(
                        "Could not parse date in IMIP email '{date}' ({ex})",
                        date=dateString, ex=e,
                    )
                    returnValue(self.UNKNOWN_TOKEN)
                now = datetime.datetime.now(dateutil.tz.tzutc())
                if dateSent < now - datetime.timedelta(hours=72):
                    returnValue(self.UNKNOWN_TOKEN_OLD)
            returnValue(self.UNKNOWN_TOKEN)

        for part in msg.walk():
            if part.get_content_type() == "text/calendar":
                calBody = part.get_payload(decode=True)
                break
        else:
            # No icalendar attachment
            log.warn(
                "Mail gateway didn't find an icalendar attachment "
                "in message {msgid}", msgid=msg['Message-ID']
            )

            toAddr = None
            fromAddr = record.attendee[7:]
            if record.organizer.startswith("mailto:"):
                toAddr = record.organizer[7:]
            elif record.organizer.startswith("urn:x-uid:"):
                uid = record.organizer[10:]
                record = yield self.directory.recordWithUID(uid)
                try:
                    if record and record.emailAddresses:
                        toAddr = list(record.emailAddresses)[0]
                except AttributeError:
                    pass

            if toAddr is None:
                log.error(
                    "Don't have an email address for the organizer; ignoring reply.")
                returnValue(self.NO_ORGANIZER_ADDRESS)

            settings = config.Scheduling["iMIP"]["Sending"]
            smtpSender = SMTPSender(
                settings.Username, settings.Password,
                settings.UseSSL, settings.Server, settings.Port)

            del msg["From"]
            msg["From"] = fromAddr
            del msg["Reply-To"]
            msg["Reply-To"] = fromAddr
            del msg["To"]
            msg["To"] = toAddr
            log.warn("Mail gateway forwarding reply back to organizer")
            yield smtpSender.sendMessage(fromAddr, toAddr, SMTPSender.betterMessageID(), msg.as_string())
            returnValue(self.REPLY_FORWARDED_TO_ORGANIZER)

        # Process the imip attachment; inject to calendar server

        log.debug(calBody)
        calendar = Component.fromString(calBody)
        event = calendar.mainComponent()

        sanitizeCalendar(calendar)

        calendar.removeAllButOneAttendee(record.attendee)
        organizerProperty = calendar.getOrganizerProperty()
        if organizerProperty is None:
            # ORGANIZER is required per rfc2446 section 3.2.3
            log.warn(
                "Mail gateway didn't find an ORGANIZER in REPLY {msgid}",
                msgid=msg['Message-ID']
            )
            event.addProperty(Property("ORGANIZER", record.organizer))
        else:
            organizerProperty.setValue(record.organizer)

        if not calendar.getAttendees():
            # The attendee we're expecting isn't there, so add it back
            # with a SCHEDULE-STATUS of SERVICE_UNAVAILABLE.
            # The organizer will then see that the reply was not successful.
            attendeeProp = Property(
                "ATTENDEE", record.attendee,
                params={
                    "SCHEDULE-STATUS": iTIPRequestStatus.SERVICE_UNAVAILABLE,
                }
            )
            event.addProperty(attendeeProp)

            # TODO: We have talked about sending an email to the reply-to
            # at this point, to let them know that their reply was missing
            # the appropriate ATTENDEE.  This will require a new localizable
            # email template for the message.

        txn = self.store.newTransaction(label="MailReceiver.processReply")
        yield txn.enqueue(
            IMIPReplyWork,
            organizer=record.organizer,
            attendee=record.attendee,
            icalendarText=str(calendar)
        )
        yield txn.commit()
        returnValue(self.INJECTION_SUBMITTED)

    # returns a deferred
    def inbound(self, message):
        """
        Given the text of an incoming message, parse and process it.
        The possible return values are:

        NO_TOKEN - there was no token in the To address
        UNKNOWN_TOKEN - there was an unknown token in the To address
        UNKNOWN_TOKEN_OLD - there was an unknown token and it's an old email
        MALFORMED_TO_ADDRESS - we could not parse the To address at all
        NO_ORGANIZER_ADDRESS - no ics attachment and no email to forward to
        REPLY_FORWARDED_TO_ORGANIZER - no ics attachment, but reply forwarded
        INJECTION_SUBMITTED - looks ok, was submitted as a work item
        INCOMPLETE_DSN - not enough in the DSN to go on
        UNKNOWN_FAILURE - any error we aren't specifically catching

        @param message: The body of the email
        @type message: C{str}
        @return: Deferred firing with one of the above action codes
        """
        try:
            msg = email.message_from_string(message)

            isDSN, action, calBody = self.checkDSN(msg)
            if isDSN:
                if action == 'failed' and calBody:
                    # This is a DSN we can handle
                    return self.processDSN(calBody, msg['Message-ID'])
                else:
                    # It's a DSN without enough to go on
                    log.error(
                        "Mail gateway can't process DSN {msgid}",
                        msgid=msg['Message-ID']
                    )
                    return succeed(self.INCOMPLETE_DSN)

            log.info(
                "Mail gateway received message {msgid} from {fromAddr} to {toAddr}",
                msgid=msg['Message-ID'], fromAddr=msg['From'], toAddr=msg['To'])

            return self.processReply(msg)

        except Exception as e:
            # Don't let a failure of any kind stop us
            log.error("Failed to process message: {error}", error=str(e))
        return succeed(self.UNKNOWN_FAILURE)


@inlineCallbacks
def injectMessage(txn, organizer, attendee, calendar):

    try:
        scheduler = IMIPScheduler(txn, None)
        results = (yield scheduler.doSchedulingDirectly("iMIP", attendee, [organizer, ], calendar,))
        log.info("Successfully injected iMIP response from {attendee} to {organizer}", attendee=attendee, organizer=organizer)
    except Exception as e:
        log.error("Failed to inject iMIP response ({error})", error=str(e))
        raise

    returnValue(results)


#
# POP3
#

class POP3DownloadProtocol(pop3client.POP3Client):
    log = Logger()

    allowInsecureLogin = False

    def serverGreeting(self, greeting):
        self.log.debug("POP servergreeting")
        pop3client.POP3Client.serverGreeting(self, greeting)
        login = self.login(
            self.factory.settings["Username"],
            self.factory.settings["Password"])
        login.addCallback(self.cbLoggedIn)
        login.addErrback(self.cbLoginFailed)

    def cbLoginFailed(self, reason):
        self.log.error(
            "POP3 login failed for {user}",
            user=self.factory.settings["Username"]
        )
        return self.quit()

    def cbLoggedIn(self, result):
        self.log.debug("POP loggedin")
        return self.listSize().addCallback(self.cbGotMessageSizes)

    def cbGotMessageSizes(self, sizes):
        self.log.debug("POP gotmessagesizes")
        downloads = []
        for i in range(len(sizes)):
            downloads.append(self.retrieve(i).addCallback(self.cbDownloaded, i))
        return defer.DeferredList(downloads).addCallback(self.cbFinished)

    @inlineCallbacks
    def cbDownloaded(self, lines, id):
        self.log.debug("POP downloaded message {msgid}", msgid=id)
        actionTaken = (yield self.factory.handleMessage("\r\n".join(lines)))

        if self.factory.deleteAllMail:
            # Delete all mail we see
            self.log.debug("POP deleting message {msgid}", msgid=id)
            self.delete(id)
        else:
            # Delete only mail we've processed
            if actionTaken == MailReceiver.INJECTION_SUBMITTED:
                self.log.debug("POP deleting message {msgid}", msgid=id)
                self.delete(id)

    def cbFinished(self, results):
        self.log.debug("POP finished")
        return self.quit()


class POP3DownloadFactory(protocol.ClientFactory):
    log = Logger()

    protocol = POP3DownloadProtocol

    def __init__(self, settings, mailReceiver, deleteAllMail):
        self.settings = settings
        self.mailReceiver = mailReceiver
        self.deleteAllMail = deleteAllMail
        self.noisy = False

    def clientConnectionLost(self, connector, reason):
        self.connector = connector
        self.log.debug("POP factory connection lost")

    def clientConnectionFailed(self, connector, reason):
        self.connector = connector
        self.log.info("POP factory connection failed")

    def handleMessage(self, message):
        self.log.debug("POP factory handle message")
        # self.log.debug(message)
        return self.mailReceiver.inbound(message)


#
# IMAP4
#


class IMAP4DownloadProtocol(imap4.IMAP4Client):
    log = Logger()

    def connectionLost(self, reason):
        if reason.type is TLSError:
            AlertPoster.postAlert("MailCertificateAlert", 7 * 24 * 60 * 60, [])

    def serverGreeting(self, capabilities):
        self.log.debug("IMAP servergreeting")
        return self.authenticate(
            self.factory.settings["Password"]
        ).addCallback(
            self.cbLoggedIn
        ).addErrback(self.ebAuthenticateFailed)

    def ebLogError(self, error):
        self.log.error("IMAP Error: {err}", err=error)

    def ebAuthenticateFailed(self, reason):
        self.log.debug(
            "IMAP authenticate failed for {name}, trying login",
            name=self.factory.settings["Username"])
        return self.login(
            self.factory.settings["Username"],
            self.factory.settings["Password"]
        ).addCallback(
            self.cbLoggedIn
        ).addErrback(self.ebLoginFailed)

    def ebLoginFailed(self, reason):
        self.log.error("IMAP login failed for {name}", name=self.factory.settings["Username"])
        self.transport.loseConnection()

    def cbLoggedIn(self, result):
        self.log.debug("IMAP logged in")
        self.select("Inbox").addCallback(self.cbInboxSelected)

    def cbInboxSelected(self, result):
        self.log.debug("IMAP Inbox selected")
        self.search(imap4.Query(unseen=True)).addCallback(self.cbGotSearch)

    def cbGotSearch(self, results):
        if results:
            ms = imap4.MessageSet()
            for n in results:
                ms.add(n)
            self.fetchUID(str(ms)).addCallback(self.cbGotUIDs)
        else:
            self.cbClosed(None)

    def cbGotUIDs(self, results):
        self.messageUIDs = [result['UID'] for result in results.values()]
        self.messageCount = len(self.messageUIDs)
        self.log.debug("IMAP Inbox has {count} unseen messages", count=self.messageCount)
        if self.messageCount:
            self.fetchNextMessage()
        else:
            # No messages; close it out
            self.close().addCallback(self.cbClosed)

    def fetchNextMessage(self):
        # self.log.debug("IMAP in fetchnextmessage")
        if self.messageUIDs:
            nextUID = self.messageUIDs.pop(0)
            messageListToFetch = imap4.MessageSet(nextUID)
            self.log.debug(
                "Downloading message {count} of {total} ({next})",
                count=self.messageCount - len(self.messageUIDs), total=self.messageCount, next=nextUID
            )
            self.fetchMessage(str(messageListToFetch), True).addCallback(
                self.cbGotMessage, messageListToFetch).addErrback(
                    self.ebLogError)
        else:
            # We're done for this polling interval
            self.expunge()

    @inlineCallbacks
    def cbGotMessage(self, results, messageList):
        self.log.debug("IMAP in cbGotMessage")
        try:
            messageData = results.values()[0]['RFC822']
        except (IndexError, KeyError):
            # results will be empty unless the "twistedmail-imap-flags-anywhere"
            # patch from http://twistedmatrix.com/trac/ticket/1105 is applied
            self.log.error("Skipping empty results -- apply twisted patch!")
            self.fetchNextMessage()
            return

        actionTaken = (yield self.factory.handleMessage(messageData))
        if self.factory.deleteAllMail:
            # Delete all mail we see
            yield self.cbFlagDeleted(messageList)
        else:
            # Delete only mail we've processed; the rest are left flagged \Seen
            if actionTaken == MailReceiver.INJECTION_SUBMITTED:
                yield self.cbFlagDeleted(messageList)
            elif actionTaken == MailReceiver.UNKNOWN_TOKEN:
                # It's not a token we recognize (probably meant for another pod)
                # so remove the \Seen flag
                yield self.cbFlagUnseen(messageList)
            elif actionTaken == MailReceiver.UNKNOWN_TOKEN_OLD:
                # It's not a token we recognize, but it's old, so delete it
                yield self.cbFlagDeleted(messageList)
            else:
                self.fetchNextMessage()

    def cbFlagUnseen(self, messageList):
        self.removeFlags(
            str(messageList), ("\\Seen",), uid=True
        ).addCallback(self.cbMessageUnseen, messageList)

    def cbMessageUnseen(self, results, messageList):
        self.log.debug("Removed \\Seen flag from message")
        self.fetchNextMessage()

    def cbFlagDeleted(self, messageList):
        self.addFlags(
            str(messageList), ("\\Deleted",), uid=True
        ).addCallback(self.cbMessageDeleted, messageList)

    def cbMessageDeleted(self, results, messageList):
        self.log.debug("Deleted message")
        self.fetchNextMessage()

    def cbClosed(self, results):
        self.log.debug("Mailbox closed")
        self.logout().addCallback(
            lambda _: self.transport.loseConnection())

    def rawDataReceived(self, data):
        # self.log.debug("RAW RECEIVED: {data}", data=data)
        imap4.IMAP4Client.rawDataReceived(self, data)

    def lineReceived(self, line):
        # self.log.debug("RECEIVED: {line}", line=line)
        imap4.IMAP4Client.lineReceived(self, line)

    def sendLine(self, line):
        # self.log.debug("SENDING: {line}", line=line)
        imap4.IMAP4Client.sendLine(self, line)


class IMAP4DownloadFactory(protocol.ClientFactory):
    log = Logger()

    protocol = IMAP4DownloadProtocol

    def __init__(self, settings, mailReceiver, deleteAllMail):
        self.log.debug("Setting up IMAPFactory")

        self.settings = settings
        self.mailReceiver = mailReceiver
        self.deleteAllMail = deleteAllMail
        self.noisy = False

    def buildProtocol(self, addr):
        p = protocol.ClientFactory.buildProtocol(self, addr)
        username = self.settings["Username"]
        p.registerAuthenticator(imap4.CramMD5ClientAuthenticator(username))
        p.registerAuthenticator(imap4.LOGINAuthenticator(username))
        p.registerAuthenticator(imap4.PLAINAuthenticator(username))
        return p

    def handleMessage(self, message):
        self.log.debug("IMAP factory handle message")
        # self.log.debug(message)
        return self.mailReceiver.inbound(message)

    def clientConnectionLost(self, connector, reason):
        self.connector = connector
        self.log.debug("IMAP factory connection lost")

    def clientConnectionFailed(self, connector, reason):
        self.connector = connector
        self.log.warn("IMAP factory connection failed")
