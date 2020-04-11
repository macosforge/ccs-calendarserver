# -*- test-case-name: twistedcaldav.test.test_stdconfig -*-
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

from os.path import basename
from plistlib import PlistParser  # @UnresolvedImport
from socket import getfqdn, gethostbyname
import copy
import os
import re
import sys

from calendarserver.push.util import getAPNTopicFromConfig

from twext.enterprise.jobs.jobitem import JobItem
from twext.enterprise.jobs.queue import ControllerQueue
from twext.enterprise.jobs.workitem import WorkItem
from twext.python.log import Logger

from twisted.logger import InvalidLogLevelError, LogLevel
from twisted.python.filepath import FilePath
from twisted.python.runtime import platform

from twistedcaldav import ical
from twistedcaldav.config import ConfigProvider, ConfigurationError, ConfigDict
from twistedcaldav.config import config, mergeData, fullServerPath
from twistedcaldav.datafilters.peruserdata import PerUserDataFilter
from twistedcaldav.util import KeychainAccessError, KeychainPasswordNotFound
from twistedcaldav.util import computeProcessCount
from twistedcaldav.util import getPasswordFromKeychain

from txdav.xml import element as davxml

from txweb2.dav.resource import TwistedACLInheritable

log = Logger()


if platform.isMacOSX():
    DEFAULT_CONFIG_FILE = "/Applications/Server.app/Contents/ServerRoot/private/etc/caldavd/caldavd-apple.plist"
else:
    DEFAULT_CONFIG_FILE = "/etc/caldavd/caldavd.plist"

DEFAULT_SERVICE_PARAMS = {
    "xml": {
        "recordTypes": ["users", "groups"],
        "xmlFile": "accounts.xml",
    },
    "opendirectory": {
        "recordTypes": ["users", "groups"],
        "node": "/Search",
    },
    "ldap": {
        "recordTypes": ["users", "groups"],
        "uri": "ldap://localhost/",
        "useTLS": False,
        "credentials": {
            "dn": None,
            "password": None,
        },
        "rdnSchema": {
            "base": "dc=example,dc=com",
            "users": "cn=users",
            "groups": "cn=groups",
            "locations": "cn=locations",
            "resources": "cn=resources",
            "addresses": "cn=addresses",
        },
        "mapping": {
            "uid": ["apple-generateduid"],
            "guid": ["apple-generateduid"],
            "shortNames": ["uid"],
            "fullNames": ["cn"],
            "emailAddresses": ["mail"],
            "memberDNs": ["uniqueMember"],
            "readOnlyProxy": ["icsSecondaryOwners"],
            "readWriteProxy": ["icsContact"],
            "loginAllowed": [],
            "hasCalendars": [],
            "autoScheduleMode": [],
            "autoAcceptGroup": [],
            "serviceNodeUID": [],
            "associatedAddress": [],
            "geographicLocation": [],
            "streetAddress": [],
        },
        "extraFilters": {
            "users": "",
            "groups": "",
            "locations": "",
            "resources": "",
            "addresses": "",
        },
        "threadPoolMax": 10,
        "authConnectionMax": 5,
        "queryConnectionMax": 5,
        "tries": 3,
        "warningThresholdSeconds": 5,
    },
}

DEFAULT_RESOURCE_PARAMS = {
    "xml": {
        "recordTypes": ["locations", "resources", "addresses"],
        "xmlFile": "resources.xml",
    },
    "opendirectory": {
        "recordTypes": ["locations", "resources", "addresses"],
        "node": "/Search",
    },
}

DEFAULT_AUGMENT_PARAMS = {
    "xml": {
        "xmlFiles": [],
        "statSeconds": 15,
    },
}


DEFAULT_DIRECTORY_ADDRESSBOOK_PARAMS = {
    "opendirectory": {
        "queryPeopleRecords": True,
        "peopleNode": "/Search/Contacts",
        "queryUserRecords": True,
        "userNode": "/Search/Contacts",
        "maxDSQueryRecords": 0,
        "queryDSLocal": False,
        "ignoreSystemRecords": True,
        "dsLocalCacheTimeout": 30,
        "liveQuery": True,
        "fakeETag": True,
        "cacheQuery": False,
        "cacheTimeout": 30,
        "standardizeSyntheticUIDs": False,
        "addDSAttrXProperties": False,
        "appleInternalServer": False,
        "additionalAttributes": [],
        "allowedAttributes": [],
    },
}

# Note: Don't use None values below; that confuses the command-line parser.
DEFAULT_CONFIG = {
    #
    # Public network address information
    #
    #    This is the server's public network address, which is provided to
    #    clients in URLs and the like.  It may or may not be the network
    #    address that the server is listening to directly, though it is by
    #    default.  For example, it may be the address of a load balancer or
    #    proxy which forwards connections to the server.
    #

    "ServerHostName": "",  # Network host name.
    "HTTPPort": 0,  # HTTP port (0 to disable HTTP)
    "SSLPort": 0,  # SSL port (0 to disable HTTPS)
    "EnableSSL": False,  # Whether to listen on SSL port(s)
    "BehindTLSProxy": False,  # Whether the service is offloading TLS duty to a proxy
    "RedirectHTTPToHTTPS": False,  # If True, all nonSSL requests redirected to an SSL Port
    "SSLMethod": "SSLv23_METHOD",  # SSLv2_METHOD, SSLv3_METHOD, SSLv23_METHOD, TLSv1_METHOD
    "SSLCiphers": "RC4-SHA:HIGH:!ADH",

    # Max-age value for Strict-Transport-Security header; set to 0 to disable header.
    "StrictTransportSecuritySeconds": 7 * 24 * 60 * 60,

    #
    # Network address configuration information.
    #
    #    This configures the actual network address that the server binds to.
    #

    "SocketFiles": {
        "Enabled": False,
        "Secured": "secured.sock",  # Socket file to listen for secure requests on
        "Unsecured": "unsecured.sock",  # Socket file to listen for insecure requests on
        "Owner": "",
        "Group": "",
        "Permissions": 0770,
    },
    "SocketRoot": "/tmp/calendarserver",

    "BindAddresses": [],  # List of IP addresses to bind to [empty = all]
    "BindHTTPPorts": [],  # List of port numbers to bind to for HTTP
    # [empty = same as "Port"]
    "BindSSLPorts": [],  # List of port numbers to bind to for SSL [empty = same as "SSLPort"]
    "InheritFDs": [],  # File descriptors to inherit for HTTP requests [empty = don't inherit]
    "InheritSSLFDs": [],  # File descriptors to inherit for HTTPS requests [empty = don't inherit]

    "UseMetaFD": True,  # Use a 'meta' FD, i.e. an FD to transmit other FDs to slave processes.
    "MetaFD": 0,  # Inherited file descriptor to call recvmsg() on to receive sockets (none = don't inherit)

    #
    # Database configuration information.
    #
    #    Defines what kind of database to use: file (deprecated) or SQL.
    #    File-based DB is only supported for migration purposes - it
    #    cannot be used for a real service.
    #
    #    For an SQL-based DB, configuration of connection parameters and various
    #    timeouts is provided.
    #

    "UseDatabase": True,  # True: database; False: files (deprecated)

    "DBType": "",  # Possible values: empty, meaning 'spawn postgres
                  # yourself', or 'postgres' or 'oracle', meaning
                  # 'connect to a postgres or Oracle database as
                  # specified by the 'DSN' configuration key.


    "DBFeatures": [  # Features supported by the database
        #
        # 'skip-locked': SKIP LOCKED available with SELECT (remove if using postgres < v9.5)
        #
        "skip-locked",
    ],

    "SpawnedDBUser": "caldav",  # The username to use when DBType is empty

    "DatabaseConnection": {  # Used to connect to an external database if DBType is non-empty
        "endpoint": "",     # Database connection endpoint
        "database": "",     # Name of database or Oracle SID
        "user": "",         # User name to connect as
        "password": "",     # Password to use
        "ssl": False,       # Set to True to require SSL (pg8000 only).
    },

    "SharedConnectionPool": False,  # Use a shared database connection pool in
    # the master process, rather than having
    # each client make its connections directly.

    "DBAMPFD": 0,  # Internally used by database to tell slave
    # processes to inherit a file descriptor and use it
    # as an AMP connection over a UNIX socket; see
    # twext.enterprise.adbapi2.ConnectionPoolConnection

    "FailIfUpgradeNeeded": True,   # Set to True to prevent the server or utility
                                   # tools from running if the database needs a schema
                                   # upgrade.

    "CheckExistingSchema": False,  # Set to True to check the current database schema
                                   # against the schema file matching the database schema
                                   # version.

    "UpgradeHomePrefix": "",    # When upgrading, only upgrade homes where the owner UID starts with
                                # the specified prefix. The upgrade will only be partial and only
                                # apply to upgrade pieces that affect entire homes. The upgrade will
                                # need to be run again without this prefix set to complete the overall
                                # upgrade.

    "TransactionTimeoutSeconds": 300,   # Timeout transactions that take longer than
                                        # the specified number of seconds. Zero means
                                        # no timeouts. 5 minute default.
    "TransactionHTTPRetrySeconds": 300,  # When a transactions times out tell HTTP clients
                                        # clients to retry after this amount of time

    #
    # Work queue configuration information
    #

    "WorkQueue": {
        "queuePollInterval": 0.1,   # Interval in seconds for job queue polling
        "queueOverdueTimeout": 300,  # Number of seconds before an assigned job is considered overdue
        "queuePollingBackoff": [     # Array of array that describe the threshold and new polling interval
                                     # for job queue polling back off
            [60, 60], [5, 1]
        ],

        "overloadLevel": 95,        # Queue capacity (percentage) which causes job processing to halt
        "highPriorityLevel": 80,    # Queue capacity (percentage) at which only high priority items are run
        "mediumPriorityLevel": 50,  # Queue capacity (percentage) at which only high/medium priority items are run

        "rowLimit": 1,              # This is used to help with concurrency problems when the underlying DB does
                                    # not support a proper "LIMIT" term with the query (Oracle). It should be set to no more than
                                    # 1 plus the number of app-servers in use. For a single app-server, always use 1.


        "failureRescheduleInterval": 60,    # When a job fails, reschedule it this number of seconds in the future
        "lockRescheduleInterval": 60,       # When a job can't run because of a lock, reschedule it this number of seconds in the future

        "workParameters": {},       # dict of work table name's, whose values are dicts containing "priority"
                                    # and "weight" items to use for newly created work.
    },

    #
    # Types of service provided
    #
    "EnableCalDAV": True,  # Enable CalDAV service
    "EnableCardDAV": True,  # Enable CardDAV service
    "MigrationOnly": False,  # When True override all other services and set the server into podding-only mode

    #
    # Data store
    #
    "ServerRoot": "/var/db/caldavd",  # The top level directory, contains (by default) ConfigRoot and DataRoot
    "DataRoot": "Data",  # Data directory, parent to DatabaseRoot, AttachmentsRoot, and others
    "DatabaseRoot": "Database",  # Database directory, contains PostgreSQL cluster
    "AttachmentsRoot": "Attachments",  # Attachments directory, where file attachments are stored
    "DocumentRoot": "Documents",  # Documents directory, contains files to be served as HTTP resources at the root level
    "ConfigRoot": "Config",  # Config directory, contains additional config files
    "LogRoot": "/var/log/caldavd",  # Log directory, contains access.log, error.log and others
    "RunRoot": "/var/run/caldavd",  # Run-time directory, contains PID files and UNIX socket files
    "WebCalendarRoot": "/Applications/Server.app/Contents/ServerRoot/usr/share/collabd/webcal/public",  # WebCal directory, contains HTML implementing the web calendar

    #
    # Quotas
    #

    # Attachments
    "UserQuota": 104857600,  # User attachment quota (in bytes - default 100MB)
    "MaximumAttachmentSize": 10485760,  # Maximum size for a single attachment (in bytes - default 10MB)
    "MaximumAttachmentsPerInstance": 5,  # Maximum number of attachments per instance

    # Resource data
    "MaxCollectionsPerHome": 50,  # Maximum number of calendars/address books allowed in a home
    "MaxResourcesPerCollection": 10000,  # Maximum number of resources in a calendar/address book
    "MaxResourceSize": 1048576,  # Maximum resource size (in bytes)
    "MaxAttendeesPerInstance": 100,  # Maximum number of unique attendees
    "MaxAllowedInstances": 3000,  # Maximum number of instances the server will index

    # Set to URL path of wiki authentication service, e.g. "/auth", in order
    # to use javascript authentication dialog.  Empty string indicates standard
    # browser authentication dialog should be used.
    "WebCalendarAuthPath": "",

    # Define mappings of URLs to file system objects (directories or files)
    "Aliases": [],

    #
    # Directory service
    #
    #    A directory service provides information about principals (e.g.
    #    users, groups, locations and resources) to the server.
    #
    "DirectoryService": {
        "Enabled": True,
        "type": "xml",
        "params": DEFAULT_SERVICE_PARAMS["xml"],
    },

    "DirectoryRealmName": "",

    # Apply an additional filter for attendee lookups where names must start
    # with the search tokens rather than just contain them.
    "DirectoryFilterStartsWith": False,

    #
    # Locations and Resources service
    #
    #    Supplements the directory service with information about locations
    #    and resources.
    #
    "ResourceService": {
        "Enabled": True,
        "type": "xml",
        "params": DEFAULT_RESOURCE_PARAMS["xml"],
    },

    #
    # Augment service
    #
    #    Augments for the directory service records to add calendar specific attributes.
    #
    "AugmentService": {
        "type": "xml",
        "params": DEFAULT_AUGMENT_PARAMS["xml"],
    },

    #
    # Proxies
    #
    "ProxyLoadFromFile": "",  # Allows for initialization of the proxy database from an XML file

    #
    # Special principals
    #
    "AdminPrincipals": [],  # Principals with "DAV:all" access (relative URLs)
    "ReadPrincipals": [],  # Principals with "DAV:read" access (relative URLs)
    "EnableProxyPrincipals": True,  # Create "proxy access" principals

    #
    # Permissions
    #
    "EnableAnonymousReadRoot": True,  # Allow unauthenticated read access to /
    "EnableAnonymousReadNav": False,  # Allow unauthenticated read access to hierarchy
    "EnablePrincipalListings": True,  # Allow listing of principal collections
    "EnableMonolithicCalendars": True,  # Render calendar collections as a monolithic iCalendar object

    #
    # Client controls
    #
    "RejectClients": [],  # List of regexes for clients to disallow

    #
    # Authentication
    #
    "Authentication": {
        "Basic": {                         # Clear text; best avoided
            "Enabled": True,
            "AllowedOverWireUnencrypted": False,  # Advertised over non-SSL?
        },
        "Digest": {                        # Digest challenge/response
            "Enabled": True,
            "Algorithm": "md5",
            "Qop": "",
            "AllowedOverWireUnencrypted": True,  # Advertised over non-SSL?
        },
        "Kerberos": {                       # Kerberos/SPNEGO
            "Enabled": False,
            "ServicePrincipal": "",
            "AllowedOverWireUnencrypted": True,  # Advertised over non-SSL?
        },
        "ClientCertificate": {                  # TLS Client Certificate
            "Enabled": False,
            "AllowedOverWireUnencrypted": True,  # Advertised over non-SSL?
            "Required": True,                   # Always require a client cert
            "CAFiles": [],                      # Array of acceptable client cert CA file names
            "SendCAsToClient": True,            # Send the list of acceptable CAs to the client
        },
        "Wiki": {
            "Enabled": False,
            "Cookie": "cc.collabd_session_guid",
            "EndpointDescriptor": "unix:path=/var/run/collabd"
        },
    },

    #
    # Logging
    #
    "AccessLogFile": "access.log",  # Apache-style access log
    "ErrorLogFile": "error.log",  # Server activity log (the verbosity is controlled by DefaultLogLevel and LogLevels keys)
    "AgentLogFile": "agent.log",  # Agent activity log (only applies to Server.app edition)
    "UtilityLogFile": "utility.log",  # Utility log (used for command line utilities; the name will be dynamically changed to that of the utility being run)
    "ErrorLogEnabled": True,  # True = use log file, False = stdout
    "ErrorLogRotateMB": 10,  # Rotate error log after so many megabytes
    "ErrorLogMaxRotatedFiles": 5,  # Retain this many error log files
    "ErrorLogRotateOnStart": False,  # Rotate error log when service starts
    "PIDFile": "caldavd.pid",
    "RotateAccessLog": False,
    "EnableExtendedAccessLog": True,
    "EnableExtendedTimingAccessLog": False,
    "DefaultLogLevel": "",  # Controls the verbosity of ErrorLogFile (valid values are error, warn, info, debug; default is info)
    "LogLevels": {},  # Allows overriding log levels on a per-package, per-module, or even per-class basis
    "LogID": "",  # Used internally to track which worker process is logging a message

    "AccountingCategories": {
        "HTTP": False,  # Log regular HTTP requests
        "iTIP": False,  # Log non-freebusy iTIP details
        "iTIP-VFREEBUSY": False,  # Log freebusy iTIP details
        "Implicit Errors": False,  # Log extra details about implicit scheduling errors
        "AutoScheduling": False,  # Log extra details about auto-accept iTIP processing
        "iSchedule": False,  # Log iSchedule HTTP requests (including cross-pod)
        "xPod": False,  # Log cross-pod conduit HTTP requests
        "Invalid Instance": False,  # Log invalid recurrence instance details
        "migration": False,  # Log cross-pod migration details
    },
    "AccountingPrincipals": [],
    "AccountingLogRoot": "accounting",  # The parent directory for accounting log directories (by default, relative to LogRoot)

    "Stats": {
        "EnableUnixStatsSocket": False,
        "UnixStatsSocket": "caldavd-stats.sock",
        "EnableTCPStatsSocket": False,
        "TCPStatsPort": 8100,
    },

    "LogDatabase": {
        "LabelsInSQL": False,
        "Statistics": False,
        "StatisticsLogFile": "sqlstats.log",
        "SQLStatements": False,
        "TransactionWaitSeconds": 0,
    },

    #
    # SSL/TLS
    #
    "SNIMode": False,
    "SSLDir": "", # Directory for housing combined key/cert/CAcert files indexed by hostname
    "DefaultSSLBundle": "", # Combined key/cert/CAcert file to fall back to when no hostname provided
    "SSLCertificate": "",  # Public key
    "SSLPrivateKey": "",  # Private key
    "SSLAuthorityChain": "",  # Certificate Authority Chain
    "SSLPassPhraseDialog": "/etc/apache2/getsslpassphrase",
    "SSLCertAdmin": "/Applications/Server.app/Contents/ServerRoot/usr/sbin/certadmin",
    "SSLKeychainIdentity": "",  # Keychain identity to use in place of cert files

    #
    # Process management
    #

    # Username and Groupname to drop privileges to, if empty privileges will not be dropped.
    "UserName": "",
    "GroupName": "",

    # Multi-process
    "ProcessType": "Combined",
    "MultiProcess": {
        "ProcessCount": 0,
        "MinProcessCount": 2,
        "PerCPU": 1,
        "PerGB": 1,
        "StaggeredStartup": {
            "Enabled": False,
            "Interval": 15,
        },
    },

    # How large a spawned process is allowed to get before it's stopped
    "MemoryLimiter": {
        "Enabled": True,
        "Seconds": 60,  # How often to check memory sizes (in seconds)
        "Bytes": 2 * 1024 * 1024 * 1024,  # Memory limit (RSS in bytes)
        "ResidentOnly": True,  # True: only take into account resident memory; False: include virtual memory
    },

    "EnableSACLs": False,  # If enabled, will honor macOS Server ACLs to control access

    "EnableReadOnlyServer": False,  # Make all data read-only

    #
    # Standard (or draft) WebDAV extensions
    #
    "EnableAddMember": True,  # POST ;add-member extension
    "EnableSyncReport": True,  # REPORT collection-sync
    "EnableSyncReportHome": True,  # REPORT collection-sync on home collections
    "EnableConfigSyncToken": True,  # Sync token includes config component
    "EnableWellKnown": True,  # /.well-known resource
    "EnableCalendarQueryExtended": True,  # Extended calendar-query REPORT

    "EnableManagedAttachments": False,  # Support Managed Attachments

    "EnableServerInfo": False,  # server-info document

    #
    # Generic CalDAV/CardDAV extensions
    #
    "EnableJSONData": True,  # Allow clients to send/receive JSON jCal and jCard format data

    #
    # Non-standard CalDAV extensions
    #
    "EnableDropBox": False,  # Calendar Drop Box
    "EnablePrivateEvents": False,  # Private Events
    "EnableTimezoneService": False,  # Old Timezone service

    "TimezoneService": {    # New standard timezone service
        "Enabled": True,      # Overall on/off switch
        "URI": "/stdtimezones",  # URI where service is hosted
        "Mode": "primary",  # Can be "primary" or "secondary"
        "BasePath": "",        # Path to directory containing a zoneinfo - if None use default package path
        # secondary service MUST define its own writable path
        "XMLInfoPath": "",        # Path to db cache info - if None use default package path
                                     # secondary service MUST define its own writable path if
                                     # not None
        "PrettyPrintJSON": True,    # User friendly JSON output

        "SecondaryService": {
            # Only one of these should be used when a secondary service is used
            "Host": "",  # Domain/IP of secondary service to discover
            "URI": "",  # HTTP(s) URI to secondary service

            "UpdateIntervalMinutes": 24 * 60,
        }
    },

    "EnableTimezonesByReference": True,  # Strip out VTIMEZONES that are known
    "UsePackageTimezones": False,  # Use timezone data from twistedcaldav.zoneinfo - don't copy to Data directory

    "EnableBatchUpload": True,  # POST batch uploads
    "MaxResourcesBatchUpload": 100,  # Maximum number of resources in a batch POST
    "MaxBytesBatchUpload": 10485760,  # Maximum size of a batch POST (10 MB)

    "Sharing": {
        "Enabled": True,  # Overall on/off switch
        "AllowExternalUsers": False,  # External (non-principal) sharees allowed

        "Calendars": {
            "Enabled": True,  # Calendar on/off switch
            "IgnorePerUserProperties": [
                "X-APPLE-STRUCTURED-LOCATION",
            ],
            "CollectionProperties": {
                "Shadowable": [
                    "{urn:ietf:params:xml:ns:caldav}calendar-description",
                ],
                "ProxyOverride": [
                    "{urn:ietf:params:xml:ns:caldav}calendar-description",
                    "{com.apple.ical:}calendarcolor",
                    "{http://apple.com/ns/ical/}calendar-color",
                    "{http://apple.com/ns/ical/}calendar-order",
                ],
                "Global": [
                ],
            },
            "Groups": {
                "Enabled": True,  # Calendar sharing to groups on/off switch
                "ReconciliationDelaySeconds": 5,
            },
        },
        "AddressBooks": {
            "Enabled": False,  # Address Book sharing on/off switch
            "CollectionProperties": {
                "Shadowable": [
                    "{urn:ietf:params:xml:ns:carddav}addressbook-description",
                ],
                "ProxyOverride": [
                ],
                "Global": [
                ],
            },
            "Groups": {
                "Enabled": False,  # Address Book Group sharing on/off switch
            },
        },
    },

    "RestrictCalendarsToOneComponentType": True,  # Only allow calendars to be created with a single component type
    # If this is on, it will also trigger an upgrade behavior that will
                                                  # split existing calendars into multiples based on component type.
                                                  # If on, it will also cause new accounts to provision with separate
                                                  # calendars for events and tasks.

    "SupportedComponents": [                     # Set of supported iCalendar components
        "VEVENT",
        "VTODO",
    ],

    "EnableTrashCollection": False,  # Enable Trash Collection
    "ExposeTrashCollection": False,  # Expose Trash Collection as a resource

    "ParallelUpgrades": False,  # Perform upgrades - currently only the
    # database to filesystem migration - but in
    # the future, hopefully all relevant
    # upgrades - in parallel in subprocesses.

    "MergeUpgrades": False,  # During the upgrade phase of startup, rather than
    # skipping homes found both on the filesystem and in
    # the database, merge the data from the filesystem
    # into the database homes.

    "EnableDefaultAlarms": True,  # Support for default alarms generated by the server
    "RemoveDuplicateAlarms": True,  # Remove duplicate alarms on PUT

    "RemoveDuplicatePrivateComments": False,  # Remove duplicate private comments on PUT

    "HostedStatus": {
        "Enabled": False,  # Decorate ATTENDEEs with the following parameter
        # to indicate where the ATTENDEE is hosted, locally
        # or externally.  It's configurable and extensible in
        # case we want to add more values.  A value of empty
        # string means no decoration.
        "Parameter": "X-APPLE-HOSTED-STATUS",
        "Values": {
            "local": "",
            "external": "EXTERNAL",
        },
    },

    "RevisionCleanup": {
        "Enabled": True,
        "SyncTokenLifetimeDays": 14.0,     # Number of days that a client sync report token is valid
        "CleanupPeriodDays": 2.0,  # Number of days between revision cleanups
    },

    "InboxCleanup": {
        "Enabled": True,
        "ItemLifetimeDays": 14.0,              # Number of days before deleting a new inbox item
        "CleanupPeriodDays": 2.0,              # Number of days between inbox cleanups
        "StartDelaySeconds": 5 * 60,            # Number of seconds before CleanupOneInboxWork starts after InboxCleanupWork
        "StaggerSeconds": 0.5,                  # Number of seconds between each CleanupOneInboxWork (fractional)
        "InboxRemoveWorkThreshold": 5,          # Number of items above which inbox removals will be deferred to a work item
        "RemovalStaggerSeconds": 0.5,           # Number of seconds between each InboxRemoveWork
    },

    # CardDAV Features
    "DirectoryAddressBook": {
        "Enabled": True,
        "type": "opendirectory",
        "params": DEFAULT_DIRECTORY_ADDRESSBOOK_PARAMS["opendirectory"],
        "name": "directory",
        "MaxQueryResults": 1000,
    },
    "EnableSearchAddressBook": False,  # /directory resource exists
    "AnonymousDirectoryAddressBookAccess": False,  # Anonymous users may access directory address book

    # /XXX CardDAV

    # Web-based administration
    "EnableWebAdmin": True,

    # JSON control API - only for testing
    "EnableControlAPI": False,

    #
    # Scheduling related options
    #

    "Scheduling": {

        "CalDAV": {
            "EmailDomain": "",  # Domain for mailto calendar user addresses on this server
            "HTTPDomain": "",  # Domain for http calendar user addresses on this server
            "AddressPatterns": [],  # Regex patterns to match local calendar user addresses
            "OldDraftCompatibility": True,  # Whether to maintain compatibility with non-implicit mode
            "ScheduleTagCompatibility": True,  # Whether to support older clients that do not use Schedule-Tag feature
            "EnablePrivateComments": True,  # Private comments from attendees to organizer
            "PerAttendeeProperties": [     # Names of iCalendar properties that are preserved when an Attendee does an invite PUT
                "X-APPLE-NEEDS-REPLY",
                "X-APPLE-TRAVEL-DURATION",
                "X-APPLE-TRAVEL-START",
                "X-APPLE-TRAVEL-RETURN-DURATION",
                "X-APPLE-TRAVEL-RETURN",
            ],
            "OrganizerPublicProperties": [     # Names of X- iCalendar properties that are sent from ORGANIZER to ATTENDEE
                "X-APPLE-DROPBOX",
                "X-APPLE-STRUCTURED-LOCATION",
            ],
            "OrganizerPublicParameters": [     # Names of X- iCalendar parameters that are sent from ORGANIZER to ATTENDEE
            ],
            "AttendeePublicProperties": [     # Names of X- iCalendar properties that are sent from ATTENDEE to ORGANIZER
                # These are also implicitly added to OrganizerPublicProperties
            ],
            "AttendeePublicParameters": [     # Names of X- iCalendar parameters that are sent from ATTENDEE to ORGANIZER
                # These are also implicitly added to OrganizerPublicParameters
            ],
        },

        "iSchedule": {
            "Enabled": False,  # iSchedule protocol
            "AddressPatterns": [],  # Reg-ex patterns to match iSchedule-able calendar user addresses
            "RemoteServers": "remoteservers.xml",  # iSchedule server configurations
            "SerialNumber": 1,  # Capabilities serial number
            "DNSDebug": "",  # File where a fake Bind zone exists for creating fake DNS results
            "DKIM": {      # DKIM options
                "Enabled": True,  # DKIM signing/verification enabled
                "Domain": "",  # Domain for DKIM (defaults to ServerHostName)
                "KeySelector": "ischedule",  # Selector for public key
                "SignatureAlgorithm": "rsa-sha256",  # Signature algorithm (one of rsa-sha1 or rsa-sha256)
                "UseDNSKey": True,  # This server's public key stored in DNS
                "UseHTTPKey": True,  # This server's public key stored in HTTP /.well-known
                "UsePrivateExchangeKey": True,  # This server's public key manually exchanged with others
                "ExpireSeconds": 3600,  # Expiration time for signature verification
                "PrivateKeyFile": "",  # File where private key is stored
                "PublicKeyFile": "",  # File where public key is stored
                "PrivateExchanges": "",  # Directory where private exchange public keys are stored
                "ProtocolDebug": False,  # Turn on protocol level debugging to return detailed information to the requestor
            },
        },

        "iMIP": {
            "Enabled": False,  # Server-to-iMIP protocol
            "Sending": {
                "Server": "",  # SMTP server to relay messages through
                "Port": 587,  # SMTP server port to relay messages through
                "Address": "",  # 'From' address for server
                "UseSSL": True,
                "Username": "",  # For account sending mail
                "Password": "",  # For account sending mail
                "SuppressionDays": 7,  # Messages for events older than this may days are not sent
            },
            "Receiving": {
                "Server": "",  # Server to retrieve email messages from
                "Port": 0,  # Server port to retrieve email messages from
                "UseSSL": True,
                "Type": "",  # Type of message access server: 'pop' or 'imap'
                "PollingSeconds": 30,  # How often to fetch mail
                "Username": "",  # For account receiving mail
                "Password": "",  # For account receiving mail
            },
            "AddressPatterns": [],  # Regex patterns to match iMIP-able calendar user addresses
            "MailTemplatesDirectory": "/Applications/Server.app/Contents/ServerRoot/usr/share/caldavd/share/email_templates",  # Directory containing HTML templates for email invitations (invite.html, cancel.html)
            "MailIconsDirectory": "/Applications/Server.app/Contents/ServerRoot/usr/share/caldavd/share/date_icons",  # Directory containing language-specific subdirectories containing date-specific icons for email invitations
            "InvitationDaysToLive": 90,  # How many days invitations are valid
        },

        "Options": {
            "AllowGroupAsOrganizer": False,  # Allow groups to be Organizers
            "AllowLocationAsOrganizer": False,  # Allow locations to be Organizers
            "AllowResourceAsOrganizer": False,  # Allow resources to be Organizers
            "AllowLocationWithoutOrganizer": True,  # Allow locations to have events without an Organizer
            "AllowResourceWithoutOrganizer": True,  # Allow resources to have events without an Organizer
            "TrackUnscheduledLocationData": True,  # Track who the last modifier of an unscheduled location event is
            "TrackUnscheduledResourceData": True,  # Track who the last modifier of an unscheduled resource event is
            "FakeResourceLocationEmail": False,  # Add fake email addresses to work around client bug
            "LimitFreeBusyAttendees": 30,  # Maximum number of attendees to request freebusy for
            "AttendeeRefreshBatch": 5,  # Number of attendees to do batched refreshes: 0 - no batching
            "AttendeeRefreshCountLimit": 50,  # Number of attendees above which attendee refreshes are suppressed: 0 - no limit
            "UIDLockTimeoutSeconds": 60,  # Time for implicit UID lock timeout
            "UIDLockExpirySeconds": 300,  # Expiration time for UID lock,
            "PrincipalHostAliases": [],  # Host names matched in http(s) CUAs
            "TimestampAttendeePartStatChanges": True,  # Add a time stamp when an Attendee changes their PARTSTAT

            "DelegeteRichFreeBusy": True,  # Delegates can get extra info in a freebusy request
            "RoomResourceRichFreeBusy": True,  # Any user can get extra info for rooms/resources in a freebusy request

            "AutoSchedule": {
                "Enabled": True,  # Auto-scheduling will never occur if set to False
                "Always": False,  # Override augments setting and always auto-schedule
                "AllowUsers": False,  # Allow auto-schedule for users
                "DefaultMode": "automatic",  # Default mode for auto-schedule processing, one of:
                # "none"            - no auto-scheduling
                # "accept-always"   - always accept, ignore busy time
                # "decline-always"  - always decline, ignore free time
                # "accept-if-free"  - accept if free, do nothing if busy
                # "decline-if-busy" - decline if busy, do nothing if free
                # "automatic"       - accept if free, decline if busy
                "FutureFreeBusyDays": 3 * 365,     # How far into the future to check for booking conflicts
            },

            "WorkQueues": {
                "Enabled": True,       # Work queues for scheduling enabled
                "RequestDelaySeconds": 5,          # Number of seconds delay for a queued scheduling request/cancel
                "ReplyDelaySeconds": 1,          # Number of seconds delay for a queued scheduling reply
                "AutoReplyDelaySeconds": 5,          # Time delay for sending an auto reply iTIP message
                "AttendeeRefreshBatchDelaySeconds": 5,          # Time after an iTIP REPLY for first batched attendee refresh
                "AttendeeRefreshBatchIntervalSeconds": 5,          # Time between attendee batch refreshes
                "TemporaryFailureDelay": 60,         # Delay in seconds before a work item is executed again after a temp failure
                "MaxTemporaryFailures": 10,         # Max number of temp failure retries before treating as a permanent failure
            },

            #
            # This controls automatic splitting of large recurring events by the
            # server. The ability for clients to split events using POST
            # ?action=split is always enabled
            #
            "Splitting": {
                "Enabled": False,          # False for now whilst we experiment with this
                "Size": 100 * 1024,     # Consider splitting when greater than 100KB
                "PastDays": 14,             # Number of days in the past where the split will occur
                "Delay": 60,             # How many seconds to delay the split work item
            }
        }
    },

    "FreeBusyURL": {
        "Enabled": False,  # Per-user free-busy-url protocol
        "TimePeriod": 14,  # Number of days into the future to generate f-b data if no explicit time-range is specified
        "AnonymousAccess": False,  # Allow anonymous read access to free-busy URL
    },

    #
    # Notifications
    #
    "Notifications": {
        "Enabled": False,
        "CoalesceSeconds": 3,

        "Services": {
            "APNS": {
                "Enabled": False,
                "SubscriptionURL": "apns",
                "SubscriptionRefreshIntervalSeconds": 2 * 24 * 60 * 60,  # How often the client should re-register (2 days)
                "SubscriptionPurgeIntervalSeconds": 12 * 60 * 60,  # How often a purge is done (12 hours)
                "SubscriptionPurgeSeconds": 14 * 24 * 60 * 60,  # How old a subscription must be before it's purged (14 days)
                "ProviderHost": "gateway.push.apple.com",
                "ProviderPort": 2195,
                "FeedbackHost": "feedback.push.apple.com",
                "FeedbackPort": 2196,
                "FeedbackUpdateSeconds": 28800,  # 8 hours
                "Environment": "PRODUCTION",
                "EnableStaggering": False,
                "StaggerSeconds": 3,
                "CalDAV": {
                    "Enabled": False,
                    "CertificatePath": "Certificates/apns:com.apple.calendar.cert.pem",
                    "PrivateKeyPath": "Certificates/apns:com.apple.calendar.key.pem",
                    "AuthorityChainPath": "Certificates/apns:com.apple.calendar.chain.pem",
                    "Passphrase": "",
                    "KeychainIdentity": "apns:com.apple.calendar",
                    "Topic": "",
                },
                "CardDAV": {
                    "Enabled": False,
                    "CertificatePath": "Certificates/apns:com.apple.contact.cert.pem",
                    "PrivateKeyPath": "Certificates/apns:com.apple.contact.key.pem",
                    "AuthorityChainPath": "Certificates/apns:com.apple.contact.chain.pem",
                    "Passphrase": "",
                    "KeychainIdentity": "apns:com.apple.contact",
                    "Topic": "",
                },
            },
            "AMP": {
                "Enabled": False,
                "Port": 62311,
                "EnableStaggering": False,
                "StaggerSeconds": 3,
            },
        }
    },

    "DirectoryProxy": {
        "Enabled": False,
        "SocketPath": "directory-proxy.sock",
        "InSidecarCachingSeconds": 120,
    },

    "DirectoryCaching": {
        "CachingSeconds": 60,               # How long to cache in worker and in memcached
        "NegativeCachingEnabled": True,
        "LookupsBetweenPurges": 10000       # 0 = purging turned off
    },

    #
    # Support multiple hosts within a domain
    #
    "Servers": {
        "Enabled": False,                   # Multiple servers enabled or not
        "ConfigFile": "localservers.xml",   # File path for server information
        "MaxClients": 5,                    # Pool size for connections between servers
        "InboxName": "podding",             # Name for top-level inbox resource
        "ConduitName": "conduit",           # Name for top-level cross-pod resource
    },

    #
    # Performance tuning
    #

    # Set the maximum number of outstanding requests to this server.
    "MaxRequests": 3,
    "MaxAccepts": 1,

    "MaxDBConnectionsPerPool": 10,  # The maximum number of outstanding database
    # connections per database connection pool.
    # When SharedConnectionPool (see above) is
    # set to True, this is the total number of
    # outgoing database connections allowed to
    # the entire server; when
    # SharedConnectionPool is False - this is the
    # default - this is the number of database
    # connections used per worker process.

    "ListenBacklog": 2024,

    "IncomingDataTimeOut": 60,          # Max. time between request lines
    "PipelineIdleTimeOut": 15,          # Max. time between pipelined requests
    "IdleConnectionTimeOut": 60 * 6,    # Max. time for response processing
    "CloseConnectionTimeOut": 15,       # Max. time for client close

    "UIDReservationTimeOut": 30 * 60,

    "MaxMultigetWithDataHrefs": 5000,
    "MaxQueryWithDataResults": 1000,

    # How many results to return for principal search REPORT requests
    "MaxPrincipalSearchReportResults": 500,

    # How many seconds to wait for principal search REPORT results
    "PrincipalSearchReportTimeout": 10,

    #
    # Client fixes per user-agent match
    #
    "ClientFixes": {
        "ForceAttendeeTRANSP": [
            "iOS/8\\.0(\\..*)?",
            "iOS/8\\.1(\\..*)?",
            "iOS/8\\.2(\\..*)?",
        ],
    },

    #
    # Localization
    #
    "Localization": {
        "TranslationsDirectory": "/Applications/Server.app/Contents/ServerRoot/usr/share/caldavd/share/translations",
        "LocalesDirectory": "locales",  # will be relative to DataRoot
        "Language": "",
    },


    #
    # Implementation details
    #
    #    The following are specific to how the server is built, and useful
    #    for development, but shouldn't be needed by users.
    #

    # Twisted
    "Twisted": {
        "reactor": "select",
    },

    # Umask
    "umask": 0022,

    # A TCP port used for communication between the child and master
    # processes (bound to 127.0.0.1). Specify 0 to let OS assign a port.
    "ControlPort": 0,

    # A unix socket used for communication between the child and master
    # processes. If blank, then an AF_INET socket is used instead.
    "ControlSocket": "caldavd.sock",

    # Support for Content-Encoding compression options as specified in RFC2616 Section 3.5
    # Defaults off, because it weakens TLS (CRIME attack).
    "ResponseCompression": False,

    # The retry-after value (in seconds) to return with a 503 error
    "HTTPRetryAfter": 180,

    # Profiling options
    "Profiling": {
        "Enabled": False,
        "BaseDirectory": "/tmp/stats",
    },

    "Memcached": {
        "MaxClients": 5,
        "Pools": {
            "Default": {
                # A unix socket used for communication with memcached.
                # If MemcacheSocket is empty string, an AF_INET socket is used.
                "MemcacheSocket": "memcache.sock",
                "ClientEnabled": True,
                "ServerEnabled": True,
                "BindAddress": "127.0.0.1",
                "Port": 11311,
                "HandleCacheTypes": [  # Possible types:
                    # "OpenDirectoryBacker",
                    # "ImplicitUIDLock",
                    # "RefreshUIDLock",
                    # "DIGESTCREDENTIALS",
                    # "resourceInfoDB",
                    # "pubsubnodes",
                    # "FBCache",
                    # "ScheduleAddressMapper",
                    # "SQL.props",
                    # "SQL.calhome",
                    # "SQL.adbkhome",
                    "Default",
                ]
            },
            # "Shared": {
            #     "ClientEnabled": True,
            #     "ServerEnabled": True,
            #     "BindAddress": "127.0.0.1",
            #     "Port": 11211,
            #     "HandleCacheTypes": [
            #         "ProxyDB",
            #         "DelegatesDB",
            #         "PrincipalToken",
            #     ]
            # },
        },
        "memcached": "memcached",  # Find in PATH
        "MaxMemory": 0,  # Megabytes
        "Options": [],
        "ProxyDBKeyNormalization": True,
    },

    "Postgres": {
        "DatabaseName": "caldav",
        "ClusterName": "cluster",
        "LogFile": "postgres.log",
        "LogRotation": False,
        "SocketDirectory": "",
        "SocketName": "",
        "ListenAddresses": [],
        "TxnTimeoutSeconds": 30,  # Time out transactions
        "SharedBuffers": 0,  # BuffersToConnectionsRatio * MaxConnections
        # Note: don't set this, it will be computed dynamically
        # See _updateMultiProcess( ) below for details
        "MaxConnections": 0,  # Dynamically computed based on ProcessCount, etc.
                             # Note: don't set this, it will be computed dynamically
                             # See _updateMultiProcess( ) below for details
        "ExtraConnections": 3,  # how many extra connections to leave for utilities
        "BuffersToConnectionsRatio": 1.5,
        "Options": [
            "-c standard_conforming_strings=on",
        ],
        "Ctl": "pg_ctl",  # If the DBType is '', and we're spawning postgres
        # ourselves, where is the pg_ctl tool to spawn it with?
        "Init": "initdb",  # If the DBType is '', and we're spawning postgres
                          # ourselves, where is the initdb tool to create its
                          # database cluster with?
    },

    "QueryCaching": {
        "Enabled": True,
        "MemcachedPool": "Default",
        "ExpireSeconds": 3600,
    },

    "GroupCaching": {
        "Enabled": True,
        "UpdateSeconds": 300,
        "UseDirectoryBasedDelegates": False,
        "InitialSchedulingDelaySeconds": 10,
        "BatchSize": 100,
        "BatchSchedulingIntervalSeconds": 2,
    },

    "GroupAttendees": {
        "Enabled": True,
        "ReconciliationDelaySeconds": 5,
        "AutoUpdateSecondsFromNow": 60 * 60,   # 1 hour
    },

    "AutomaticPurging": {
        "Enabled": True,
        "PollingIntervalSeconds": 7 * 24 * 60 * 60,   # 7 days
        "CheckStaggerSeconds": 0,  # No staggering
        "PurgeIntervalSeconds": 7 * 24 * 60 * 60,   # 7 days
        "HomePurgeDelaySeconds": 60,
        "GroupPurgeIntervalSeconds": 7 * 24 * 60 * 60,   # 7 days
    },

    "Manhole": {
        "Enabled": False,
        "UseSSH": True,  # Set to False for telnet
        "StartingPortNumber": 5000,  # Master listens here, children increment
        "DPSPortNumber": 4999,  # Directory Proxy listens here
        "PasswordFilePath": "",  # Path to password file with lines of user:pass
        "sshKeyName": "manhole.key",  # Relative to DataRoot
        "sshKeySize": 4096,
    },

    "EnableKeepAlive": False,

    "EnableResponseCache": True,
    "ResponseCacheTimeout": 30,  # Minutes

    "EnableFreeBusyCache": True,
    "FreeBusyCacheDaysBack": 7,
    "FreeBusyCacheDaysForward": 12 * 7,

    "FreeBusyIndexLowerLimitDays": 365,
    "FreeBusyIndexExpandAheadDays": 365,
    "FreeBusyIndexExpandMaxDays": 5 * 365,
    "FreeBusyIndexDelayedExpand": False,
    "FreeBusyIndexSmartUpdate": True,

    # The RootResource uses a twext property store. Specify the class here
    "RootResourcePropStoreClass": "txweb2.dav.xattrprops.xattrPropertyStore",

    # Used in the command line utilities to specify which service class to use to carry out work.
    "UtilityServiceClass": "",

    # Inbox items created more than MigratedInboxDaysCutoff days in the past are removed during migration
    "MigratedInboxDaysCutoff": 60,

    # The default timezone for the server; on OS X you can leave this empty and the
    # system's timezone will be used.  If empty and not on OS X it will default to
    # America/Los_Angeles.
    "DefaultTimezone": "",

    # After this many seconds of no admin requests, shutdown the agent.  Zero
    # means no automatic shutdown.
    "AgentInactivityTimeoutSeconds": 5 * 60,

    # Program to execute if the service cannot start; for example in OS X we
    # want to call serveradmin to disable the service so launchd does not keep
    # respawning it.  Empty string to disable this feature.
    "ServiceDisablingProgram": "",

    # Program to execute to post an alert to the administrator; for example in
    # OS X we want to call calendarserver_alert <alert-type> <args>
    "AlertPostingProgram": "",

    # These three keys are relative to ConfigRoot:

    # Config to read first and merge
    "ImportConfig": "",

    # Other plists to parse after this one; note that an Include can change
    # the ServerRoot and/or ConfigRoot, thereby affecting the locations of
    # the following Includes in the list. (Useful for service directory
    # relocation)
    "Includes": [],

    # Which config file calendarserver_config should  write to for changes;
    # empty string means the main config file
    "WritableConfigFile": "",
}


class NoUnicodePlistParser(PlistParser):
    """
    A variant of L{PlistParser} which avoids exposing the 'unicode' data-type
    to application code when non-ASCII characters are found, instead
    consistently exposing UTF-8 encoded 'str' objects.
    """

    def getData(self):
        """
        Get the currently-parsed data as a 'str' object.
        """
        data = "".join(self.data).encode("utf-8")
        self.data = []
        return data


class PListConfigProvider(ConfigProvider):

    def loadConfig(self):
        configDict = {}
        if self._configFileName:
            configDict = self._parseConfigFromFile(self._configFileName)
        configDict = ConfigDict(configDict)

        def _loadImport(childDict):
            # Look for an import and read that one as the main config and merge the current one into that
            if "ImportConfig" in childDict and childDict.ImportConfig:
                if childDict.ImportConfig[0] != ".":
                    configRoot = os.path.join(childDict.ServerRoot, childDict.ConfigRoot)
                    path = _expandPath(fullServerPath(configRoot, childDict.ImportConfig))
                else:
                    path = childDict.ImportConfig
                if os.path.exists(path):
                    importDict = ConfigDict(self._parseConfigFromFile(path))
                    if importDict:
                        self.importedFiles.append(path)
                        importDict = _loadImport(importDict)
                        mergeData(importDict, childDict)
                        return importDict
                raise ConfigurationError("Import configuration file '{path}' must exist and be valid.".format(path=path))
            else:
                return childDict

        def _loadIncludes(parentDict):
            # Now check for Includes and parse and add each of those
            if "Includes" in parentDict:
                for include in parentDict.Includes:
                    # Recompute this because ServerRoot and ConfigRoot
                    # could change when including another file
                    configRoot = os.path.join(parentDict.ServerRoot,
                                              parentDict.ConfigRoot)
                    path = _expandPath(fullServerPath(configRoot, include))
                    if os.path.exists(path):
                        additionalDict = ConfigDict(self._parseConfigFromFile(path))
                        if additionalDict:
                            self.includedFiles.append(path)
                            _loadIncludes(additionalDict)
                            mergeData(parentDict, additionalDict)
                    else:
                        self.missingFiles.append(path)

        configDict = _loadImport(configDict)
        _loadIncludes(configDict)
        return configDict

    def _parseConfigFromFile(self, filename):
        parser = NoUnicodePlistParser()
        configDict = None
        try:
            with open(filename) as f:
                configDict = parser.parse(f)
        except (IOError, OSError):
            log.error("Configuration file does not exist or is inaccessible: {f}", f=filename)
            raise ConfigurationError("Configuration file does not exist or is inaccessible: %s" % (filename,))
        else:
            configDict = _cleanup(configDict, self._defaults)
        return configDict


def _expandPath(path):
    if '$' in path:
        return path.replace('$', getfqdn())
    elif '#' in path:
        return path.replace('#', gethostbyname(getfqdn()))
    else:
        return path

RELATIVE_PATHS = [
    ("ServerRoot", "DataRoot"),
    ("ServerRoot", "ConfigRoot"),
    ("ServerRoot", "LogRoot"),
    ("ServerRoot", "RunRoot"),
    ("DataRoot", "DocumentRoot"),
    ("DataRoot", "DatabaseRoot"),
    ("DataRoot", "AttachmentsRoot"),
    ("DataRoot", ("TimezoneService", "BasePath",)),
    ("DataRoot", ("Localization", "LocalesDirectory",)),
    ("ConfigRoot", ("Scheduling", "iSchedule", "DNSDebug",)),
    ("ConfigRoot", ("Scheduling", "iSchedule", "DKIM", "PrivateKeyFile",)),
    ("ConfigRoot", ("Scheduling", "iSchedule", "DKIM", "PublicKeyFile",)),
    ("ConfigRoot", ("Scheduling", "iSchedule", "DKIM", "PrivateExchanges",)),
    ("ConfigRoot", "WritableConfigFile"),
    ("ConfigRoot", ("Notifications", "Services", "APNS", "CalDAV", "AuthorityChainPath",)),
    ("ConfigRoot", ("Notifications", "Services", "APNS", "CalDAV", "CertificatePath",)),
    ("ConfigRoot", ("Notifications", "Services", "APNS", "CalDAV", "PrivateKeyPath",)),
    ("ConfigRoot", ("Notifications", "Services", "APNS", "CardDAV", "AuthorityChainPath",)),
    ("ConfigRoot", ("Notifications", "Services", "APNS", "CardDAV", "CertificatePath",)),
    ("ConfigRoot", ("Notifications", "Services", "APNS", "CardDAV", "PrivateKeyPath",)),
    ("LogRoot", "AccessLogFile"),
    ("LogRoot", "ErrorLogFile"),
    ("LogRoot", "AgentLogFile"),
    ("LogRoot", "UtilityLogFile"),
    ("LogRoot", ("Postgres", "LogFile",)),
    ("LogRoot", ("LogDatabase", "StatisticsLogFile",)),
    ("LogRoot", "AccountingLogRoot"),
    ("RunRoot", "PIDFile"),
    ("RunRoot", ("Stats", "UnixStatsSocket",)),
    ("RunRoot", "ControlSocket"),
    ("RunRoot", ("Memcached", "Pools", "Default", "MemcacheSocket")),
    ("RunRoot", ("DirectoryProxy", "SocketPath",)),
    ("SocketRoot", ("SocketFiles", "Secured")),
    ("SocketRoot", ("SocketFiles", "Unsecured")),
]


def _updateDataStore(configDict, reloading=False):
    """
    Post-update configuration hook for making all configured paths relative to
    their respective root directories rather than the current working directory.
    """
    # Remove possible trailing slash from ServerRoot
    try:
        configDict["ServerRoot"] = configDict["ServerRoot"].rstrip("/")
    except KeyError:
        pass

    for root, relativePath in RELATIVE_PATHS:
        if root in configDict:
            if isinstance(relativePath, str):
                relativePath = (relativePath,)

            inDict = configDict
            for segment in relativePath[:-1]:
                if segment not in inDict:
                    inDict = None
                    break
                inDict = inDict[segment]
            lastPath = relativePath[-1]
            relativePath = ".".join(relativePath)
            if inDict and lastPath in inDict:
                previousAbsoluteName = ".absolute." + relativePath
                previousRelativeName = ".relative." + relativePath

                # If we previously made the name absolute, and the name in the
                # config is still the same absolute name that we made it, let's
                # change it to be the relative name again.  (This is necessary
                # because the config data is actually updated several times before
                # the config *file* has been read, so these keys will be made
                # absolute based on default values, and need to be made relative to
                # non-default values later.)  -glyph
                if previousAbsoluteName in configDict and (
                        configDict[previousAbsoluteName] == inDict[lastPath]
                ):
                    userSpecifiedPath = configDict[previousRelativeName]
                else:
                    userSpecifiedPath = inDict[lastPath]
                    configDict[previousRelativeName] = inDict[lastPath]
                newAbsolutePath = fullServerPath(configDict[root],
                                                 userSpecifiedPath)
                inDict[lastPath] = newAbsolutePath
                configDict[previousAbsoluteName] = newAbsolutePath


def _updateHostName(configDict, reloading=False):
    if not configDict.ServerHostName:
        hostname = getfqdn()
        if not hostname:
            hostname = "localhost"
        configDict.ServerHostName = hostname

    # Default DirectoryRealmName from ServerHostName
    if not configDict.DirectoryRealmName:
        # Use system-wide realm on OSX
        try:
            import ServerFoundation
            realmName = ServerFoundation.XSAuthenticator.defaultRealm()
            configDict.DirectoryRealmName = realmName
        except ImportError:
            configDict.DirectoryRealmName = configDict.ServerHostName


def _updateWorkQueue(configDict, reloading=False):
    try:
        # Initialize queue polling parameters from config settings
        for attr in (
            "queuePollInterval",
            "queueOverdueTimeout",
            "queuePollingBackoff",
            "overloadLevel",
            "highPriorityLevel",
            "mediumPriorityLevel",
            "rowLimit",
        ):
            setattr(ControllerQueue, attr, getattr(config.WorkQueue, attr))

        # Initialize job parameters from config settings
        for attr in (
            "failureRescheduleInterval",
            "lockRescheduleInterval",
        ):
            setattr(JobItem, attr, getattr(config.WorkQueue, attr))

        # Work parameters
        WorkItem.updateWorkTypes(configDict.WorkQueue.workParameters)
    except Exception:
        log.failure("Unable to apply config.workParameters changes")


def _updateMultiProcess(configDict, reloading=False):
    """
    Dynamically compute ProcessCount if it's set to 0.  Always compute
    MaxConnections and SharedBuffers based on ProcessCount, ExtraConnections,
    SharedConnectionPool, MaxDBConnectionsPerPool, and BuffersToConnectionsRatio
    """
    if configDict.MultiProcess.ProcessCount == 0:
        processCount = computeProcessCount(
            configDict.MultiProcess.MinProcessCount,
            configDict.MultiProcess.PerCPU,
            configDict.MultiProcess.PerGB,
        )
        configDict.MultiProcess.ProcessCount = processCount

    # Start off with extra connections to be used by command line utilities and
    # administration/inspection tools
    maxConnections = configDict.Postgres.ExtraConnections

    if configDict.SharedConnectionPool:
        # If SharedConnectionPool is enabled, then only the master process will
        # be connection to the database, therefore use MaxDBConnectionsPerPool
        maxConnections += configDict.MaxDBConnectionsPerPool
    else:
        # Otherwise the master *and* each worker process will be connecting
        maxConnections += (
            (configDict.MultiProcess.ProcessCount + 1) *
            configDict.MaxDBConnectionsPerPool
        )

    configDict.Postgres.MaxConnections = maxConnections
    configDict.Postgres.SharedBuffers = int(
        configDict.Postgres.MaxConnections *
        configDict.Postgres.BuffersToConnectionsRatio
    )


def _preUpdateDirectoryService(configDict, items, reloading=False):
    # Special handling for directory services configs
    dsType = items.get("DirectoryService", {}).get("type", None)
    if dsType is None:
        dsType = configDict.DirectoryService.type
    else:
        if dsType == configDict.DirectoryService.type:
            oldParams = configDict.DirectoryService.params
            newParams = items.DirectoryService.get("params", {})
            mergeData(oldParams, newParams)
        else:
            if dsType in DEFAULT_SERVICE_PARAMS:
                configDict.DirectoryService.params = copy.deepcopy(DEFAULT_SERVICE_PARAMS[dsType])
            else:
                configDict.DirectoryService.params = {}

    for param in items.get("DirectoryService", {}).get("params", {}):
        if dsType in DEFAULT_SERVICE_PARAMS and param not in DEFAULT_SERVICE_PARAMS[dsType]:
            log.warn("Parameter {p} is not supported by service {t}", p=param, t=dsType)


def _postUpdateDirectoryService(configDict, reloading=False):
    if configDict.DirectoryService.type in DEFAULT_SERVICE_PARAMS:
        for param in tuple(configDict.DirectoryService.params):
            if param not in DEFAULT_SERVICE_PARAMS[configDict.DirectoryService.type]:
                del configDict.DirectoryService.params[param]


def _preUpdateResourceService(configDict, items, reloading=False):
    # Special handling for directory services configs
    dsType = items.get("ResourceService", {}).get("type", None)
    if dsType is None:
        dsType = configDict.ResourceService.type
    else:
        if dsType == configDict.ResourceService.type:
            oldParams = configDict.ResourceService.params
            newParams = items.ResourceService.get("params", {})
            mergeData(oldParams, newParams)
        else:
            if dsType in DEFAULT_RESOURCE_PARAMS:
                configDict.ResourceService.params = copy.deepcopy(DEFAULT_RESOURCE_PARAMS[dsType])
            else:
                configDict.ResourceService.params = {}

    for param in items.get("ResourceService", {}).get("params", {}):
        if dsType in DEFAULT_RESOURCE_PARAMS and param not in DEFAULT_RESOURCE_PARAMS[dsType]:
            log.warn("Parameter {p} is not supported by service {t}", p=param, t=dsType)


def _postUpdateResourceService(configDict, reloading=False):
    if configDict.ResourceService.type in DEFAULT_RESOURCE_PARAMS:
        for param in tuple(configDict.ResourceService.params):
            if param not in DEFAULT_RESOURCE_PARAMS[configDict.ResourceService.type]:
                del configDict.ResourceService.params[param]

    # Upgrading resources.xml must be done prior to using the store/directory
    if configDict.ResourceService.Enabled and configDict.ResourceService.type == "xml":
        resourcesFileName = configDict.ResourceService.params.xmlFile
        if resourcesFileName[0] not in ("/", "."):
            resourcesFileName = os.path.join(configDict.DataRoot, resourcesFileName)
        resourcesFilePath = FilePath(resourcesFileName)
        if resourcesFilePath.exists():
            from twistedcaldav.upgrade import upgradeResourcesXML
            upgradeResourcesXML(resourcesFilePath)


def _preUpdateDirectoryAddressBookBackingDirectoryService(configDict, items, reloading=False):
    #
    # Special handling for directory address book configs
    #
    dsType = items.get("DirectoryAddressBook", {}).get("type", None)
    if dsType is None:
        dsType = configDict.DirectoryAddressBook.type
    else:
        if dsType == configDict.DirectoryAddressBook.type:
            oldParams = configDict.DirectoryAddressBook.params
            newParams = items["DirectoryAddressBook"].get("params", {})
            mergeData(oldParams, newParams)
        else:
            if dsType in DEFAULT_DIRECTORY_ADDRESSBOOK_PARAMS:
                configDict.DirectoryAddressBook.params = copy.deepcopy(DEFAULT_DIRECTORY_ADDRESSBOOK_PARAMS[dsType])
            else:
                configDict.DirectoryAddressBook.params = {}

    for param in items.get("DirectoryAddressBook", {}).get("params", {}):
        if param not in DEFAULT_DIRECTORY_ADDRESSBOOK_PARAMS[dsType]:
            raise ConfigurationError("Parameter %s is not supported by service %s" % (param, dsType))

    mergeData(configDict, items)

    for param in tuple(configDict.DirectoryAddressBook.params):
        if param not in DEFAULT_DIRECTORY_ADDRESSBOOK_PARAMS[configDict.DirectoryAddressBook.type]:
            del configDict.DirectoryAddressBook.params[param]


def _postUpdateAugmentService(configDict, reloading=False):
    if configDict.AugmentService.type in DEFAULT_AUGMENT_PARAMS:
        for param in tuple(configDict.AugmentService.params):
            if param not in DEFAULT_AUGMENT_PARAMS[configDict.AugmentService.type]:
                log.warn("Parameter {p} is not supported by service {t}", p=param, t=configDict.AugmentService.type)
                del configDict.AugmentService.params[param]

    # Upgrading augments.xml must be done prior to using the store/directory
    if configDict.AugmentService.type == "xml":
        for fileName in configDict.AugmentService.params.xmlFiles:
            if fileName[0] not in ("/", "."):
                fileName = os.path.join(configDict.DataRoot, fileName)
            filePath = FilePath(fileName)
            if filePath.exists():
                from twistedcaldav.upgrade import upgradeAugmentsXML
                upgradeAugmentsXML(filePath)


def _updateACLs(configDict, reloading=False):
    #
    # Base resource ACLs
    #
    def readOnlyACE(allowAnonymous):
        if allowAnonymous:
            reader = davxml.All()
        else:
            reader = davxml.Authenticated()

        return davxml.ACE(
            davxml.Principal(reader),
            davxml.Grant(
                davxml.Privilege(davxml.Read()),
                davxml.Privilege(davxml.ReadCurrentUserPrivilegeSet()),
            ),
            davxml.Protected(),
        )

    configDict.AdminACEs = tuple(
        davxml.ACE(
            davxml.Principal(davxml.HRef(admin_principal)),
            davxml.Grant(davxml.Privilege(davxml.All())),
            davxml.Protected(),
            TwistedACLInheritable(),
        )
        for admin_principal in configDict.AdminPrincipals
    )

    configDict.ReadACEs = tuple(
        davxml.ACE(
            davxml.Principal(davxml.HRef(read_principal)),
            davxml.Grant(
                davxml.Privilege(davxml.Read()),
                davxml.Privilege(davxml.ReadCurrentUserPrivilegeSet()),
            ),
            davxml.Protected(),
            TwistedACLInheritable(),
        )
        for read_principal in configDict.ReadPrincipals
    )

    configDict.RootResourceACL = davxml.ACL(
        # Read-only for anon or authenticated, depending on config
        readOnlyACE(configDict.EnableAnonymousReadRoot),

        # Add inheritable all access for admins
        * configDict.AdminACEs
    )

    log.debug("Root ACL: {x}", x=configDict.RootResourceACL.toxml())

    configDict.ProvisioningResourceACL = davxml.ACL(
        # Read-only for anon or authenticated, depending on config
        readOnlyACE(configDict.EnableAnonymousReadNav),

        # Add read and read-acl access for admins
        * [
            davxml.ACE(
                davxml.Principal(davxml.HRef(_principal)),
                davxml.Grant(
                    davxml.Privilege(davxml.Read()),
                    davxml.Privilege(davxml.ReadACL()),
                    davxml.Privilege(davxml.ReadCurrentUserPrivilegeSet()),
                ),
                davxml.Protected(),
            )
            for _principal in configDict.AdminPrincipals
        ]
    )

    log.debug("Nav ACL: {x}", x=configDict.ProvisioningResourceACL.toxml())

    def principalObjects(urls):
        for principalURL in urls:
            yield davxml.Principal(davxml.HRef(principalURL))

    # Should be sets, except WebDAVElement isn't hashable.
    a = configDict.AdminPrincipalObjects = list(
        principalObjects(configDict.AdminPrincipals))
    b = configDict.ReadPrincipalObjects = list(
        principalObjects(configDict.ReadPrincipals))
    configDict.AllAdminPrincipalObjects = a + b


def _updateRejectClients(configDict, reloading=False):
    #
    # Compile RejectClients expressions for speed
    #
    try:
        configDict.RejectClients = [re.compile(x) for x in configDict.RejectClients if x]
    except re.error, e:
        raise ConfigurationError("Invalid regular expression in RejectClients: %s" % (e,))


def _updateClientFixes(configDict, reloading=False):
    #
    # Compile ClientFixes expressions for speed
    #
    try:
        configDict.ClientFixesCompiled = {}
        for key, expressions in configDict.ClientFixes.items():
            configDict.ClientFixesCompiled[key] = [re.compile("^{}$".format(x)) for x in expressions]
    except re.error, e:
        raise ConfigurationError("Invalid regular expression in ClientFixes: %s" % (e,))


def _updateUtilityLog(configDict, reloading=False):
    # Change just the file name to match the process - leave directory as-is
    configDict["UtilityLogFile"] = os.path.join(os.path.dirname(configDict["UtilityLogFile"]), "{}.log".format(basename(sys.argv[0])))


def _updateLogLevels(configDict, reloading=False):
    log.levels().clearLogLevels()

    try:
        if "DefaultLogLevel" in configDict:
            levelName = configDict["DefaultLogLevel"]
            if levelName:
                log.levels().setLogLevelForNamespace(
                    None, LogLevel.levelWithName(levelName)
                )

        if "LogLevels" in configDict:
            for namespace, levelName in configDict["LogLevels"].iteritems():
                log.levels().setLogLevelForNamespace(
                    namespace, LogLevel.levelWithName(levelName)
                )

    except InvalidLogLevelError, e:
        raise ConfigurationError("Invalid log level: %s" % (e.level))


def _updateNotifications(configDict, reloading=False):

    # These three keys go into producing the config sync token
    config.addSyncTokenKey("Notifications.Services.APNS.Enabled")
    config.addSyncTokenKey("Notifications.Services.APNS.CalDAV.Topic")
    config.addSyncTokenKey("Notifications.Services.APNS.CardDAV.Topic")

    # Reloading not supported -- requires process running as root
    if reloading:
        return

    for _ignore_key, service in configDict.Notifications["Services"].iteritems():
        if service["Enabled"]:
            configDict.Notifications["Enabled"] = True
            break
    else:
        configDict.Notifications["Enabled"] = False

    for key, service in configDict.Notifications["Services"].iteritems():

        if (key == "APNS" and service["Enabled"]):
            # Retrieve APN topics from certificates if not explicitly set
            for protocol, accountName in (
                ("CalDAV", "apns:com.apple.calendar"),
                ("CardDAV", "apns:com.apple.contact"),
            ):

                # Set protocol-specific Enabled key only if that protocol's
                # service is actually enabled
                if configDict["Enable{}".format(protocol)]:
                    service[protocol].Enabled = True
                else:
                    continue

                if service[protocol].Enabled:
                    if not service[protocol]["Topic"]:
                        try:
                            getAPNTopicFromConfig(protocol, accountName, service[protocol])
                        except ValueError as e:
                            log.error(e)

                    # If we already have the cert passphrase, don't fetch it again
                    if not service[protocol]["Passphrase"]:
                        log.info("{p} APNS certificate passphrase not found in keychain", p=protocol)


def _updateICalendar(configDict, reloading=False):
    """
    Updated support iCalendar components.
    """
    ical._updateAllowedComponents(tuple(configDict.SupportedComponents))


def _updateScheduling(configDict, reloading=False):
    #
    # Scheduling
    #

    # Reloading not supported -- requires process running as root
    if reloading:
        return

    service = configDict.Scheduling["iMIP"]

    if service["Enabled"]:

        for direction in ("Sending", "Receiving"):
            if service[direction].Username:
                # Get password from keychain.  If not there, fall back to
                # what is in the plist.
                try:
                    account = "%s@%s" % (
                        service[direction].Username,
                        service[direction].Server
                    )
                    if not service[direction]["Password"]:
                        password = getPasswordFromKeychain(account)
                        service[direction]["Password"] = password
                        log.info("iMIP {d} password successfully retrieved from keychain", d=direction)
                except KeychainAccessError:
                    # The system doesn't support keychain
                    pass
                except KeychainPasswordNotFound:
                    # The password doesn't exist in the keychain.
                    log.info("iMIP {d} password not found in keychain", d=direction)


def _updateSharing(configDict, reloading=False):
    #
    # Sharing
    #

    # Transfer configured non-per-user property names to PerUserDataFilter
    for propertyName in configDict.Sharing.Calendars.IgnorePerUserProperties:
        PerUserDataFilter.IGNORE_X_PROPERTIES.append(propertyName)


def _updateCompliance(configDict, reloading=False):
    from twistedcaldav.serverinfo import buildServerInfo
    (configDict.CalDAVComplianceClasses,
        configDict.ServerInfo,
        configDict.ServerInfoToken) = buildServerInfo(configDict)

    # Make all the ServerInfo config feed into the config portion of the sync
    # token as well:
    config.addSyncTokenKey("ServerInfoToken")


PRE_UPDATE_HOOKS = (
    _preUpdateDirectoryService,
    _preUpdateResourceService,
    _preUpdateDirectoryAddressBookBackingDirectoryService,
)
POST_UPDATE_HOOKS = (
    _updateMultiProcess,
    _updateUtilityLog,  # Must be before _updateDataStore for proper relative path
    _updateDataStore,
    _updateHostName,
    _updateWorkQueue,
    _postUpdateDirectoryService,
    _postUpdateResourceService,
    _postUpdateAugmentService,
    _updateACLs,
    _updateRejectClients,
    _updateClientFixes,
    _updateLogLevels,
    _updateNotifications,
    _updateICalendar,
    _updateScheduling,
    _updateSharing,
    _updateCompliance,
)


def _cleanup(configDict, defaultDict):
    cleanDict = copy.deepcopy(configDict)

    def unknown(key):
        config_key = "CALENDARSERVER_CONFIG_VALIDATION"
        config_key_value = "loose"
        if config_key in os.environ and os.environ[config_key] == config_key_value:
            pass
        else:
            log.error("Ignoring unknown configuration option: {k}", k=key)
            del cleanDict[key]

    def deprecated(oldKey, newKey):
        log.error("Configuration option {o} is deprecated in favor of {n}.", o=oldKey, n=newKey)
        if oldKey in configDict and newKey in configDict:
            raise ConfigurationError(
                "Both %r and %r options are specified; use the %r option only."
                % (oldKey, newKey, newKey)
            )

    def renamed(oldKey, newKey):
        deprecated(oldKey, newKey)
        cleanDict[newKey] = configDict[oldKey]
        del cleanDict[oldKey]

    renamedOptions = {
        # "BindAddress": "BindAddresses",
    }

    for key in configDict:
        if key in defaultDict:
            continue

        elif key in renamedOptions:
            renamed(key, renamedOptions[key])

        else:
            unknown(key,)

    return cleanDict

config.setProvider(PListConfigProvider(DEFAULT_CONFIG))
config.addPreUpdateHooks(PRE_UPDATE_HOOKS)
config.addPostUpdateHooks(POST_UPDATE_HOOKS)
config.update()


def _preserveConfig(configDict):
    """
    Preserve certain config keys across reset( ) because these can't be
    re-fetched after the process has shed privileges
    """
    iMIP = configDict.Scheduling.iMIP
    preserved = {
        "MailSendingPassword": iMIP.Sending.Password,
        "MailReceivingPassword": iMIP.Receiving.Password,
    }
    return preserved


def _restoreConfig(configDict, preserved):
    """
    Restore certain config keys across reset( ) because these can't be
    re-fetched after the process has shed privileges
    """
    iMIP = configDict.Scheduling.iMIP
    iMIP.Sending.Password = preserved["MailSendingPassword"]
    iMIP.Receiving.Password = preserved["MailReceivingPassword"]


config.addResetHooks(_preserveConfig, _restoreConfig)
