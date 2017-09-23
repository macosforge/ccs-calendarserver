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

"""
Outbound IMIP mail handling for Calendar Server
"""

from __future__ import with_statement

from cStringIO import StringIO
import os
import re

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import email.utils
from pycalendar.datetime import DateTime
from pycalendar.duration import Duration
from twext.enterprise.dal.record import fromTable
from twext.enterprise.jobs.workitem import WorkItem
from twext.python.log import Logger
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.mail.smtp import rfc822date
from twisted.web.microdom import Text as DOMText, Element as DOMElement
from twisted.web.microdom import parseString
from twisted.web.template import XMLString, TEMPLATE_NAMESPACE, Element, renderer, flattenString, tags
from twistedcaldav.config import config
from twistedcaldav.ical import Component
from twistedcaldav.localization import translationTo, _, getLanguage
from txdav.caldav.datastore.scheduling.utils import normalizeCUAddr
from txdav.caldav.datastore.scheduling.imip.smtpsender import SMTPSender
from txdav.common.datastore.sql_tables import schema


log = Logger()


EMBEDDED_HEADER_CHECK = re.compile(r'\n[^ \t]+:')


""" SCHEMA:
create sequence WORKITEM_SEQ;

create table IMIP_INVITATION_WORK (
  WORK_ID         integer primary key default nextval('WORKITEM_SEQ') not null,
  NOT_BEFORE      timestamp    default timezone('UTC', CURRENT_TIMESTAMP),
  FROM_ADDR       varchar(255) not null,
  TO_ADDR         varchar(255) not null,
  ICALENDAR_TEXT  text         not null
);
"""


class IMIPInvitationWork(WorkItem, fromTable(schema.IMIP_INVITATION_WORK)):
    """
    Sends outbound IMIP messages
    """

    mailSender = None

    @classmethod
    def getMailSender(cls):
        """
        Instantiate and return a singleton MailSender object
        @return: a MailSender
        """
        if cls.mailSender is None:
            if config.Scheduling.iMIP.Enabled:
                settings = config.Scheduling.iMIP.Sending
                smtpSender = SMTPSender(
                    settings.Username, settings.Password,
                    settings.UseSSL, settings.Server, settings.Port)
                cls.mailSender = MailSender(
                    settings.Address,
                    settings.SuppressionDays, smtpSender, getLanguage(config))
        return cls.mailSender

    @inlineCallbacks
    def doWork(self):
        """
        Send an outbound IMIP message
        """
        mailSender = self.getMailSender()
        if mailSender is not None:
            calendar = Component.fromString(self.icalendarText)
            yield mailSender.outbound(
                self.transaction,
                self.fromAddr, self.toAddr, calendar)

#
# Templates
#

plainCancelTemplate = u"""%(subject)s

%(orgLabel)s: %(plainOrganizer)s
%(dateLabel)s: %(dateInfo)s %(recurrenceInfo)s
%(timeLabel)s: %(timeInfo)s %(durationInfo)s
"""

plainInviteTemplate = u"""%(subject)s

%(orgLabel)s: %(plainOrganizer)s
%(locLabel)s: %(location)s
%(dateLabel)s: %(dateInfo)s %(recurrenceInfo)s
%(timeLabel)s: %(timeInfo)s %(durationInfo)s
%(descLabel)s: %(description)s
%(urlLabel)s: %(url)s
%(attLabel)s: %(plainAttendees)s
"""


htmlCancelTemplate = u"""<html>
    <body><div>

    <h1>%(subject)s</h1>
    <p>
    <h3>%(orgLabel)s:</h3> %(htmlOrganizer)s
    </p>
    <p>
    <h3>%(dateLabel)s:</h3> %(dateInfo)s %(recurrenceInfo)s
    </p>
    <p>
    <h3>%(timeLabel)s:</h3> %(timeInfo)s %(durationInfo)s
    </p>
    """.encode("utf-8")


htmlInviteTemplate = u"""<html>
    <body><div>
    <p>%(inviteLabel)s</p>

    <h1>%(summary)s</h1>
    <p>
    <h3>%(orgLabel)s:</h3> %(htmlOrganizer)s
    </p>
    <p>
    <h3>%(locLabel)s:</h3> %(location)s
    </p>
    <p>
    <h3>%(dateLabel)s:</h3> %(dateInfo)s %(recurrenceInfo)s
    </p>
    <p>
    <h3>%(timeLabel)s:</h3> %(timeInfo)s %(durationInfo)s
    </p>
    <p>
    <h3>%(descLabel)s:</h3> %(description)s
    </p>
    <p>
    <h3>%(urlLabel)s:</h3> <a href="%(url)s">%(url)s</a>
    </p>
    <p>
    <h3>%(attLabel)s:</h3> %(htmlAttendees)s
    </p>
    """.encode("utf-8")


def _visit(document, node):
    if isinstance(node, DOMText):
        idx = node.parentNode.childNodes.index(node)
        splitted = node.data.split("%(")
        firstTextNode = document.createTextNode(splitted[0])
        firstTextNode.parentNode = node.parentNode
        replacements = [firstTextNode]
        for moreText in splitted[1:]:
            slotName, extra = moreText.split(')', 1)
            extra = extra[1:]
            slotElement = document.createElement('t:slot')
            slotElement.setAttribute("name", slotName)
            slotElement.parentNode = node.parentNode
            textNode = document.createTextNode(extra)
            textNode.parentNode = node.parentNode
            replacements.append(slotElement)
            replacements.append(textNode)
        node.parentNode.childNodes[idx:idx + 1] = replacements

    elif isinstance(node, DOMElement):
        for attrName, attrVal in node.attributes.items():
            if '%(' in attrVal:
                del node.attributes[attrName]
                elem = document.createElement('t:attr')
                elem.setAttribute('name', attrName)
                textNode = document.createTextNode(attrVal)
                elem.appendChild(textNode)
                node.appendChild(elem)


def _walk(document, n):
    _visit(document, n)
    for subn in n.childNodes:
        _walk(document, subn)


def _fixup(data, rendererName):
    document = parseString(data, beExtremelyLenient=True)
    document.documentElement.setAttribute(
        "xmlns:t", TEMPLATE_NAMESPACE
    )
    document.doctype = (
        'html PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd"'
    )
    document.documentElement.setAttribute(
        "t:render", rendererName
    )
    _walk(document, document)
    result = document.toxml()
    return result


class StringFormatTemplateLoader(object):
    """
    Loader for twisted.web.template that converts a template with %()s slots.
    """

    def __init__(self, fileFactory, rendererName):
        """
        @param fileFactory: a 1-argument callable which returns a file-like
            object that contains the %()s-format template.

        @param rendererName: the name of the renderer.

        @type rendererName: C{str}
        """
        self.fileFactory = fileFactory
        self.rendererName = rendererName

    def load(self):
        html = _fixup(self.fileFactory().read(), self.rendererName)
        return XMLString(html).load()


def localizedLabels(language, canceled, inviteState):
    """
    Generate localized labels for an email in the given language.

    @param language: a 2-letter language code

    @type language: C{str}

    @return: a 2-tuple of (subjectFormatString, labelDict), where the first is a
        format string for use in the subject, and the latter is a dictionary
        with labels suitable for filling out HTML and plain-text templates.  All
        values are C{str}s.
    """
    with translationTo(language):
        if canceled:
            subjectFormatString = _("Event canceled: %(summary)s")
        elif inviteState == "new":
            subjectFormatString = _("Event invitation: %(summary)s")
        elif inviteState == "update":
            subjectFormatString = _("Event update: %(summary)s")
        else:
            subjectFormatString = _("Event reply: %(summary)s")

        if canceled:
            inviteLabel = _("Event Canceled")
        else:
            if inviteState == "new":
                inviteLabel = _("Event Invitation")
            elif inviteState == "update":
                inviteLabel = _("Event Update")
            else:
                inviteLabel = _("Event Reply")

        labels = dict(
            dateLabel=_("Date"),
            timeLabel=_("Time"),
            durationLabel=_("Duration"),
            recurrenceLabel=_("Occurs"),
            descLabel=_("Description"),
            urlLabel=_("URL"),
            orgLabel=_("Organizer"),
            attLabel=_("Attendees"),
            locLabel=_("Location"),
            inviteLabel=inviteLabel,
        )

        # The translations we get back from gettext are utf-8 encoded
        # strings, so convert to unicode
        for key in labels.keys():
            if isinstance(labels[key], str):
                labels[key] = labels[key].decode("utf-8")

    return subjectFormatString.decode("utf-8"), labels


class MailSender(object):
    """
    Generates outbound IMIP messages and sends them.
    """
    log = Logger()

    def __init__(self, address, suppressionDays, smtpSender, language):
        self.address = address
        self.suppressionDays = suppressionDays
        self.smtpSender = smtpSender
        self.language = language

    @inlineCallbacks
    def outbound(self, txn, originator, recipient, calendar, onlyAfter=None):
        """
        Generates and sends an outbound IMIP message.

        @param txn: the transaction to use for looking up/creating tokens
        @type txn: L{CommonStoreTransaction}
        """

        if onlyAfter is None:
            duration = Duration(days=self.suppressionDays)
            onlyAfter = DateTime.getNowUTC() - duration

        icaluid = calendar.resourceUID()
        method = calendar.propertyValue("METHOD")

        # Clean up the attendee list which is purely used within the human
        # readable email message (not modifying the calendar body)
        attendees = []
        for attendeeProp in calendar.getAllAttendeeProperties():
            cutype = attendeeProp.parameterValue("CUTYPE", "INDIVIDUAL")
            if cutype == "INDIVIDUAL":
                cn = attendeeProp.parameterValue("CN", None)
                if cn is not None:
                    cn = cn.decode("utf-8")
                cuaddr = normalizeCUAddr(attendeeProp.value())
                if cuaddr.startswith("mailto:"):
                    mailto = cuaddr[7:]
                    if not cn:
                        cn = mailto
                else:
                    emailAddress = attendeeProp.parameterValue("EMAIL", None)
                    if emailAddress:
                        mailto = emailAddress
                    else:
                        mailto = None

                if cn or mailto:
                    attendees.append((cn, mailto))

        toAddr = recipient
        if not recipient.lower().startswith("mailto:"):
            raise ValueError("ATTENDEE address '%s' must be mailto: for iMIP "
                             "operation." % (recipient,))
        recipient = recipient[7:]

        if method != "REPLY":
            # Invites and cancellations:

            # Reuse or generate a token based on originator, toAddr, and
            # event uid
            record = (yield txn.imipGetToken(originator, toAddr.lower(), icaluid))
            if record is None:

                # Because in the past the originator was sometimes in mailto:
                # form, lookup an existing token by mailto: as well
                organizerProperty = calendar.getOrganizerProperty()
                organizerEmailAddress = organizerProperty.parameterValue("EMAIL", None)
                if organizerEmailAddress is not None:
                    record = (yield txn.imipGetToken("mailto:%s" % (organizerEmailAddress.lower(),), toAddr.lower(), icaluid))

            if record is None:
                record = (yield txn.imipCreateToken(originator, toAddr.lower(), icaluid))
                self.log.debug(
                    "Mail gateway created token {token}for {orig} "
                    "(originator), {recip} (recipient) and {uid} (icaluid)",
                    token=record.token, orig=originator, recip=toAddr, uid=icaluid,
                )
                inviteState = "new"

            else:
                self.log.debug(
                    "Mail gateway reusing token {token} for {orig} "
                    "(originator), {recip} (recipient) and {uid} (icaluid)",
                    token=record.token, orig=originator, recip=toAddr, uid=icaluid,
                )
                inviteState = "update"
            token = record.token

            fullServerAddress = self.address
            _ignore_name, serverAddress = email.utils.parseaddr(fullServerAddress)
            pre, post = serverAddress.split('@')
            addressWithToken = "%s+%s@%s" % (pre, token, post)

            organizerProperty = calendar.getOrganizerProperty()
            organizerEmailAddress = organizerProperty.parameterValue("EMAIL",
                                                                     None)
            organizerValue = organizerProperty.value()
            organizerProperty.setValue("mailto:%s" % (addressWithToken,))

            # If the organizer is also an attendee, update that attendee value
            # to match
            organizerAttendeeProperty = calendar.getAttendeeProperty(
                [organizerValue])
            if organizerAttendeeProperty is not None:
                organizerAttendeeProperty.setValue("mailto:%s" %
                                                   (addressWithToken,))

            # The email's From will include the originator's real name email
            # address if available.  Otherwise it will be the server's email
            # address (without # + addressing)
            if organizerEmailAddress:
                orgEmail = fromAddr = organizerEmailAddress
            else:
                fromAddr = serverAddress
                orgEmail = None
            cn = calendar.getOrganizerProperty().parameterValue('CN', None)
            if cn is None:
                cn = u'Calendar Server'
                orgCN = orgEmail
            else:
                orgCN = cn = cn.decode("utf-8")

            # a unicode cn (rather than an encode string value) means the
            # from address will get properly encoded per rfc2047 within the
            # MIMEMultipart in generateEmail
            formattedFrom = "%s <%s>" % (cn, fromAddr)

            # Reply-to address will be the server+token address

        else:  # REPLY
            inviteState = "reply"

            # Look up the attendee property corresponding to the originator
            # of this reply
            originatorAttendeeProperty = calendar.getAttendeeProperty(
                [originator])
            formattedFrom = fromAddr = originator = ""
            if originatorAttendeeProperty:
                originatorAttendeeEmailAddress = (
                    originatorAttendeeProperty.parameterValue("EMAIL", None)
                )
                if originatorAttendeeEmailAddress:
                    formattedFrom = fromAddr = originator = (
                        originatorAttendeeEmailAddress
                    )

            organizerMailto = str(calendar.getOrganizer())
            if not organizerMailto.lower().startswith("mailto:"):
                raise ValueError("ORGANIZER address '%s' must be mailto: "
                                 "for REPLY." % (organizerMailto,))
            orgEmail = organizerMailto[7:]

            orgCN = calendar.getOrganizerProperty().parameterValue('CN', None)
            if orgCN:
                orgCN = orgCN.decode("utf-8")
            addressWithToken = formattedFrom

        # At the point we've created the token in the db, which we always
        # want to do, but if this message is for an event completely in
        # the past we don't want to actually send an email.
        if not calendar.hasInstancesAfter(onlyAfter):
            self.log.debug("Skipping IMIP message for old event")
            returnValue(True)

        # Now prevent any "internal" CUAs from being exposed by converting
        # to mailto: if we have one
        for attendeeProp in calendar.getAllAttendeeProperties():
            cutype = attendeeProp.parameterValue('CUTYPE', None)
            if cutype == "INDIVIDUAL":
                cuaddr = normalizeCUAddr(attendeeProp.value())
                if not cuaddr.startswith("mailto:"):
                    emailAddress = attendeeProp.parameterValue("EMAIL", None)
                    if emailAddress:
                        attendeeProp.setValue("mailto:%s" % (emailAddress,))

        msgId, message = self.generateEmail(
            inviteState, calendar, orgEmail,
            orgCN, attendees, formattedFrom, addressWithToken, recipient,
            language=self.language)

        try:
            success = (yield self.smtpSender.sendMessage(
                fromAddr, toAddr, msgId, message))
            returnValue(success)
        except Exception as e:
            self.log.error("Failed to send IMIP message ({ex})", ex=str(e))
            returnValue(False)

    def _scrubHeader(self, value):
        """
        Check for what would be considered "embedded headers" and, if present,
        remove the newlines.
        """
        if EMBEDDED_HEADER_CHECK.search(value) is not None:
            value = value.replace("\n", " ")
        return value

    def generateEmail(self, inviteState, calendar, orgEmail, orgCN,
                      attendees, fromAddress, replyToAddress, toAddress,
                      language='en'):
        """
        Generate MIME text containing an iMIP invitation, cancellation, update
        or reply.

        @param inviteState: 'new', 'update', or 'reply'.

        @type inviteState: C{str}

        @param calendar: the iCalendar component to attach to the email.

        @type calendar: L{twistedcaldav.ical.Component}

        @param orgEmail: The email for the organizer, in C{localhost@domain}
            format, or C{None} if the organizer has no email address.

        @type orgEmail: C{str} or C{NoneType}

        @param orgCN: Common name / display name for the organizer.

        @type orgCN: C{unicode}

        @param attendees: A C{list} of 2-C{tuple}s of (common name, email
            address) similar to (orgEmail, orgCN).

        @param fromAddress: the address to use in the C{From:} header of the
            email.

        @type fromAddress: C{str}

        @param replyToAddress: the address to use in the C{Reply-To} header.

        @type replyToAddress: C{str}

        @param toAddress: the address to use in the C{To} header.

        @type toAddress: C{str}

        @param language: a 2-letter language code describing the target
            language that the email should be generated in.

        @type language: C{str}

        @return: a 2-tuple of C{str}s: (message ID, message text).  The message
            ID is the value of the C{Message-ID} header, and the message text is
            the full MIME message, ready for transport over SMTP.
        """

        details = self.getEventDetails(calendar, language=language)
        canceled = (calendar.propertyValue("METHOD") == "CANCEL")

        subjectFormat, labels = localizedLabels(language, canceled, inviteState)
        details.update(labels)

        details['subject'] = subjectFormat % {'summary': details['summary']}

        plainText = self.renderPlainText(details, (orgCN, orgEmail),
                                         attendees, canceled)

        htmlText = self.renderHTML(
            details, (orgCN, orgEmail), attendees, canceled
        )

        msg = MIMEMultipart()
        msg["From"] = fromAddress
        msg["Subject"] = self._scrubHeader(details['subject'])
        msg["Reply-To"] = self._scrubHeader(replyToAddress)
        msg["To"] = self._scrubHeader(toAddress)
        msg["Date"] = rfc822date()
        msgId = SMTPSender.betterMessageID()
        msg["Message-ID"] = msgId

        msgAlt = MIMEMultipart("alternative")
        msg.attach(msgAlt)

        # plain version
        msgPlain = MIMEText(plainText, "plain", "UTF-8")
        msgAlt.attach(msgPlain)

        # html version
        msgHtmlRelated = MIMEMultipart("related", type="text/html")
        msgAlt.attach(msgHtmlRelated)

        msgHtml = MIMEText(htmlText, "html", "UTF-8")
        msgHtmlRelated.attach(msgHtml)

        # the icalendar attachment

        # Make sure we always have the timezones used in the calendar data as iMIP requires VTIMEZONE
        # always be present (i.e., timezones-by-reference is not allowed in iMIP).
        calendarText = calendar.getTextWithTimezones(includeTimezones=True)

        self.log.debug("Mail gateway sending calendar body: {body}", body=calendarText)
        msgIcal = MIMEText(calendarText, "calendar", "UTF-8")
        method = calendar.propertyValue("METHOD").lower()
        msgIcal.set_param("method", method)
        msgIcal.add_header("Content-ID", "<invitation.ics>")
        msgIcal.add_header(
            "Content-Disposition",
            "inline;filename=invitation.ics")
        msg.attach(msgIcal)

        return msgId, msg.as_string()

    def renderPlainText(self, details, xxx_todo_changeme, attendees, canceled):
        """
        Render text/plain message part based on invitation details and a flag
        indicating whether the message is a cancellation.

        @return: UTF-8 encoded text.

        @rtype: C{str}
        """
        (orgCN, orgEmail) = xxx_todo_changeme
        plainAttendeeList = []
        for cn, mailto in attendees:
            if cn:
                plainAttendeeList.append(
                    cn if not mailto else "%s <%s>" % (cn, mailto)
                )
            elif mailto:
                plainAttendeeList.append("<%s>" % (mailto,))

        details['plainAttendees'] = ", ".join(plainAttendeeList)

        details['plainOrganizer'] = (
            orgCN if not orgEmail else "%s <%s>" % (orgCN, orgEmail)
        )

        # plain text version
        if canceled:
            plainTemplate = plainCancelTemplate
        else:
            plainTemplate = plainInviteTemplate

        return (plainTemplate % details).encode("UTF-8")

    def renderHTML(self, details, organizer, attendees, canceled):
        """
        Render HTML message part based on invitation details and a flag
        indicating whether the message is a cancellation.

        @return: html text (C{str}, representing utf-8 encoded bytes)).
        """
        orgCN, orgEmail = organizer

        # TODO: htmlAttendees needs to be a separate element with a separate
        # template fragment.  Luckily that fragment is the same regardless
        # of the rest of the template.
        htmlAttendees = []
        first = True
        for cn, mailto in attendees:
            if not first:
                htmlAttendees.append(u", ")
            else:
                first = False

            if mailto:
                if not cn:
                    cn = mailto
                htmlAttendees.append(
                    tags.a(href="mailto:%s" % (mailto,))(cn)
                )
            else:
                htmlAttendees.append(cn)

        details['htmlAttendees'] = htmlAttendees

        # TODO: htmlOrganizer is also some HTML that requires additional
        # template stuff, and once again, it's just a 'mailto:'.
        # tags.a(href="mailto:"+email)[cn]
        if orgEmail:
            if not orgCN:
                orgCN = orgEmail
            details['htmlOrganizer'] = tags.a(href="mailto:%s" % (orgEmail,))(orgCN)
        else:
            details['htmlOrganizer'] = orgCN

        templateDir = config.Scheduling.iMIP.MailTemplatesDirectory.rstrip("/")
        templateName = "cancel.html" if canceled else "invite.html"
        templatePath = os.path.join(templateDir, templateName)

        if not os.path.exists(templatePath):
            # Fall back to built-in simple templates:
            if canceled:
                htmlTemplate = htmlCancelTemplate
            else:
                htmlTemplate = htmlInviteTemplate
        else:  # HTML template file exists

            with open(templatePath) as templateFile:
                htmlTemplate = templateFile.read()

        class EmailElement(Element):
            loader = StringFormatTemplateLoader(lambda: StringIO(htmlTemplate),
                                                "email")

            @renderer
            def email(self, request, tag):
                return tag.fillSlots(**details)

        textCollector = []
        flattenString(None, EmailElement()).addCallback(textCollector.append)
        htmlText = textCollector[0]
        return htmlText

    def getEventDetails(self, calendar, language='en'):
        """
        Create a dictionary mapping slot names - specifically: summary,
        description, location, dateInfo, timeInfo, durationInfo, recurrenceInfo,
        url - with localized string values that should be placed into the HTML
        and plain-text templates.

        @param calendar: a L{Component} upon which to base the language.
        @type calendar: L{Component}

        @param language: a 2-letter language code.
        @type language: C{str}

        @return: a mapping from template slot name to localized text.
        @rtype: a C{dict} mapping C{bytes} to C{unicode}.
        """

        # Get the most appropriate component
        component = calendar.mainComponent()

        results = {}

        dtStart = component.propertyValue('DTSTART')
        results['month'] = dtStart.getMonth()
        results['day'] = dtStart.getDay()

        for propertyToResult in ['summary', 'description', 'location', 'url']:
            result = component.propertyValue(propertyToResult.upper())
            if result is None:
                result = u""
            else:
                result = result.decode('utf-8')
            results[propertyToResult] = result

        with translationTo(language) as trans:
            results['dateInfo'] = trans.date(component).decode('utf-8')
            results['timeInfo'], duration = (x.decode('utf-8') for x in trans.time(component))
            results['durationInfo'] = u"(%s)" % (duration,) if duration else u""

            for propertyName in ('RRULE', 'RDATE', 'EXRULE', 'EXDATE',
                                 'RECURRENCE-ID'):
                if component.hasProperty(propertyName):
                    results['recurrenceInfo'] = _("(Repeating)").decode('utf-8')
                    break
            else:
                results['recurrenceInfo'] = u""

        return results
