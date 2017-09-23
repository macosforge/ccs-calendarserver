#
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

from pycalendar.datetime import DateTime
from pycalendar.duration import Duration
from pycalendar.period import Period
from pycalendar.timezone import Timezone

from twext.python.log import Logger
from txweb2.http import HTTPError

from twisted.internet.defer import inlineCallbacks, returnValue

from twistedcaldav import customxml
from twistedcaldav.accounting import emitAccounting, accountingEnabled
from twistedcaldav.config import config
from twistedcaldav.ical import Property, DTSTAMP_PARAM
from twistedcaldav.instance import InvalidOverriddenInstanceError

from txdav.caldav.datastore.scheduling.freebusy import FreebusyQuery
from txdav.caldav.datastore.scheduling.itip import iTipProcessing, iTIPRequestStatus, \
    iTipGenerator
from txdav.caldav.datastore.scheduling.utils import getCalendarObjectForRecord
from txdav.caldav.datastore.scheduling.utils import normalizeCUAddr
from txdav.caldav.datastore.scheduling.work import ScheduleRefreshWork, \
    ScheduleAutoReplyWork
from txdav.caldav.icalendarstore import ComponentUpdateState, \
    ComponentRemoveState, QueryMaxResources
from txdav.who.idirectory import AutoScheduleMode

import collections
import hashlib
import json
import uuid

"""
CalDAV implicit processing.

This module handles the processing of scheduling messages being delivered to a calendar user's inbox.
It determines who is scheduling (organizer or attendee) and applies the scheduling message changes
to the recipient's calendar data as well as depositing the scheduling message in the inbox. For users
who have an auto-accept option on, it will also handle the automatic response. Also, refreshes of other
attendees (when one attendee replies) are triggered from here.
"""

__all__ = [
    "ImplicitProcessor",
    "ImplicitProcessorException",
]

log = Logger()


class ImplicitProcessorException(Exception):

    def __init__(self, msg):
        self.msg = msg


class ImplicitProcessor(object):

    def __init__(self):
        pass

    @inlineCallbacks
    def doImplicitProcessing(self, txn, message, originator, recipient, noAttendeeRefresh=False):
        """
        Do implicit processing of a scheduling message, and possibly also auto-process it
        if the recipient has auto-accept on.

        @param message: the iTIP message
        @type message: L{twistedcaldav.ical.Component}
        @param originator: calendar user sending the message
        @type originator: C{str}
        @param recipient: calendar user receiving the message
        @type recipient: C{str}

        @return: a C{tuple} of (C{bool}, C{bool}, C{bool}, C{bool}) indicating whether the message was processed,
            and if it was whether auto-processing has taken place, whether it needs to be stored in the inbox, and
            the changes property for the inbox item.
        """

        self.txn = txn
        self.message = message
        self.originator = originator
        self.recipient = recipient
        self.noAttendeeRefresh = noAttendeeRefresh

        # TODO: for now going to assume that the originator is local - i.e. the scheduling message sent
        # represents the actual organizer's view.

        # First see whether this is the organizer or attendee sending the message
        self.extractCalendarData()

        if self.isOrganizerReceivingMessage():
            result = (yield self.doImplicitOrganizer())
        elif self.isAttendeeReceivingMessage():
            try:
                result = (yield self.doImplicitAttendee())
            except ImplicitProcessorException:
                # These we always pass up
                raise
            except Exception as e:
                # We attempt to recover from this. That involves trying to re-write the attendee data
                # to match that of the organizer assuming we have the organizer's full data available, then
                # we try the processing operation again.
                log.failure("{processor}.doImplicitAttendee()", processor=self)
                log.error("ImplicitProcessing - originator '{orig}' to recipient '{recip}' with UID: '{uid}' - exception raised will try to fix: {ex}", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid, ex=e)
                result = (yield self.doImplicitAttendeeEventFix(e))
                if result:
                    log.error("ImplicitProcessing - originator '{orig}' to recipient '{recip}' with UID: '{uid}' - restored organizer's copy", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
                    try:
                        result = (yield self.doImplicitAttendee())
                    except Exception as e:
                        log.failure("{processor}.doImplicitAttendee()", processor=self)
                        log.error("ImplicitProcessing - originator '{orig}' to recipient '{recip}' with UID: '{uid}' - exception raised after fix: {ex}", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid, ex=e)
                        raise ImplicitProcessorException("5.1;Service unavailable")
                else:
                    log.error("ImplicitProcessing - originator '{orig}' to recipient '{recip}' with UID: '{uid}' - could not fix", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
                    raise ImplicitProcessorException("5.1;Service unavailable")
        else:
            log.error("METHOD:{method} not supported for implicit scheduling.", method=self.method)
            raise ImplicitProcessorException("3.14;Unsupported capability")

        returnValue(result)

    def extractCalendarData(self):

        # Some other useful things
        self.method = self.message.propertyValue("METHOD")
        self.uid = self.message.resourceUID()

    def isOrganizerReceivingMessage(self):
        return self.method in ("REPLY", "REFRESH", "X-RESTORE")

    def isAttendeeReceivingMessage(self):
        return self.method in ("REQUEST", "ADD", "CANCEL", "POLLSTATUS")

    @inlineCallbacks
    def getRecipientsCopy(self):
        """
        Get the Recipient's copy of the event being processed.
        """

        self.recipient_calendar = None
        self.recipient_calendar_resource = None
        calendar_resource = (yield getCalendarObjectForRecord(self.txn, self.recipient.record, self.uid))
        if calendar_resource:
            self.recipient_calendar = (yield calendar_resource.componentForUser(self.recipient.record.uid)).duplicate()
            self.recipient_calendar_resource = calendar_resource
            self.recipient_in_trash = self.recipient_calendar_resource.isInTrash()

    @inlineCallbacks
    def doImplicitOrganizer(self):
        """
        Process an iTIP message sent to the organizer.
        """

        # Locate the organizer's copy of the event.
        yield self.getRecipientsCopy()
        if self.recipient_calendar is None or self.recipient_in_trash:
            log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' ignoring UID: '{uid}' - organizer has no copy", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
            returnValue((True, True, False, None,))

        # Handle new items differently than existing ones.
        if self.method in ("REPLY", "X-RESTORE"):
            result = (yield self.doImplicitOrganizerUpdate())
        elif self.method == "REFRESH":
            # With implicit we ignore refreshes.
            # TODO: for iMIP etc we do need to handle them
            result = (True, True, False, None,)

        returnValue(result)

    @inlineCallbacks
    def doImplicitOrganizerUpdate(self):
        """
        An iTIP REPLY has been sent by an attendee to an organizer and the attendee state needs to be sync'd
        to the organizer's copy of the event.
        """

        # Check to see if this is a valid reply - this will also merge the changes to the organizer's copy
        result, processed = iTipProcessing.processReply(self.message, self.recipient_calendar)
        if result:

            # Let the store know that no time-range info has changed
            self.recipient_calendar.noInstanceIndexing = True

            # Update the organizer's copy of the event
            log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' processing METHOD:REPLY, UID: '{uid}' - updating event", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
            self.organizer_calendar_resource = (yield self.writeCalendarResource(None, self.recipient_calendar_resource, self.recipient_calendar))
            self.organizer_uid = self.organizer_calendar_resource.parentCollection().ownerHome().uid()
            self.organizer_calendar_resource_id = self.organizer_calendar_resource.id()

            organizer = self.recipient_calendar.getOrganizer()

            attendeeReplying, rids = processed

            if self.method == "X-RESTORE":
                changes = None
                refreshNeeded = True
                yield self._doRefresh(self.organizer_calendar_resource, only_attendees=(attendeeReplying,))

            else:
                # Build the schedule-changes XML element
                refreshNeeded = False
                reply_details = (customxml.Attendee.fromString(attendeeReplying),)

                for rid, reply_changes in sorted(rids):
                    recurrence = []
                    if rid == "":
                        recurrence.append(customxml.Master())
                    else:
                        recurrence.append(customxml.RecurrenceID.fromString(rid))
                    changes = []

                    for param in reply_changes.params:
                        changes.append(customxml.ChangedProperty(customxml.ChangedParameter(name=param), name="ATTENDEE"))
                        refreshNeeded = True

                    for prop in reply_changes.props:
                        changes.append(customxml.ChangedProperty(name=prop))

                    recurrence.append(customxml.Changes(*changes))
                    reply_details += (customxml.Recurrence(*recurrence),)

                changes = customxml.ScheduleChanges(
                    customxml.DTStamp(),
                    customxml.Action(
                        customxml.Reply(*reply_details),
                    ),
                )

            # Only update other attendees when the partstat was changed by the reply,
            # and only if the request does not indicate we should skip attendee refresh
            # (e.g. inbox item processing during migration from non-implicit server)
            if refreshNeeded and not self.noAttendeeRefresh:
                # Check limit of attendees
                if config.Scheduling.Options.AttendeeRefreshCountLimit == 0 or len(self.recipient_calendar.getAllUniqueAttendees()) <= config.Scheduling.Options.AttendeeRefreshCountLimit:
                    yield self.queueAttendeeUpdate((attendeeReplying, organizer,))

            result = (True, False, True, changes,)

        else:
            # Ignore scheduling message
            result = (True, True, False, None,)

        returnValue(result)

    @inlineCallbacks
    def queueAttendeeUpdate(self, exclude_attendees):
        """
        Queue up a background update to attendees.

        @param exclude_attendees: list of attendees who should not be refreshed (e.g., the one that triggered the refresh)
        @type exclude_attendees: C{list}
        """

        self.uid = self.recipient_calendar.resourceUID()

        # Check for batched refreshes
        if config.Scheduling.Options.AttendeeRefreshBatch:
            # Batch refresh those attendees that need it.
            allAttendees = sorted(list(self.recipient_calendar.getAllUniqueAttendees()))
            allAttendees = filter(lambda x: x not in exclude_attendees, allAttendees)
            if allAttendees:
                yield self._enqueueBatchRefresh(allAttendees)
        else:
            yield self._doRefresh(self.organizer_calendar_resource, exclude_attendees)

    def _enqueueBatchRefresh(self, attendees):
        """
        Create a batch refresh work item. Do this in a separate method to allow for easy
        unit testing.

        @param attendees: the list of attendees to refresh
        @type attendees: C{list}
        """
        return ScheduleRefreshWork.refreshAttendees(
            self.txn,
            self.recipient_calendar_resource,
            self.recipient_calendar,
            attendees,
        )

    @inlineCallbacks
    def _doRefresh(self, organizer_resource, exclude_attendees=(), only_attendees=None):
        """
        Do a refresh of attendees.

        @param organizer_resource: the resource for the organizer's calendar data
        @type organizer_resource: L{DAVResource}
        @param exclude_attendees: list of attendees to not refresh
        @type exclude_attendees: C{tuple}
        @param only_attendees: list of attendees to refresh (C{None} - refresh all)
        @type only_attendees: C{tuple}
        """
        log.debug("ImplicitProcessing - refreshing UID: '{uid}', Attendees: {att}", uid=self.uid, att=", ".join(only_attendees) if only_attendees else "all")
        from txdav.caldav.datastore.scheduling.implicit import ImplicitScheduler
        scheduler = ImplicitScheduler()
        yield scheduler.refreshAllAttendeesExceptSome(
            self.txn,
            organizer_resource,
            exclude_attendees,
            only_attendees=only_attendees,
        )

    @inlineCallbacks
    def doImplicitAttendee(self):
        """
        Process an iTIP message sent to an attendee.
        """

        # Locate the attendee's copy of the event if it exists.
        yield self.getRecipientsCopy()
        self.new_resource = self.recipient_calendar is None

        # If we get a CANCEL and we don't have a matching resource already stored, simply
        # ignore the CANCEL.
        if self.new_resource and self.method == "CANCEL":
            log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' ignoring METHOD:CANCEL, UID: '{uid}' - attendee has no copy", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
            result = (True, True, False, None)
        else:
            result = (yield self.doImplicitAttendeeUpdate())

        returnValue(result)

    @inlineCallbacks
    def doImplicitAttendeeUpdate(self):
        """
        An iTIP message has been sent to an attendee by the organizer. We need to update the attendee state
        based on the nature of the iTIP message.
        """

        # Do security check: ORGANZIER in iTIP MUST match existing resource value
        if self.recipient_calendar:
            existing_organizer = self.recipient_calendar.getOrganizer()
            existing_organizer = normalizeCUAddr(existing_organizer) if existing_organizer else ""
            new_organizer = normalizeCUAddr(self.message.getOrganizer())
            new_organizer = normalizeCUAddr(new_organizer) if new_organizer else ""
            if existing_organizer != new_organizer:
                # Additional check - if the existing organizer is missing and the originator
                # is local to the server - then allow the change
                if not (existing_organizer == "" and self.originator.hosted()):
                    log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' ignoring UID: '{uid}' - organizer has no copy", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
                    # If the recipient copy is in the trash, just remove it
                    if self.recipient_in_trash:
                        yield self.deleteCalendarResource(self.recipient_calendar_resource)
                        # Reset state to make it look like a new iTIP being processed
                        self.recipient_calendar = None
                        self.recipient_calendar_resource = None
                        self.recipient_in_trash = False
                        self.new_resource = True
                    else:
                        raise ImplicitProcessorException("5.3;Organizer change not allowed")

                # For a missing existing organizer we don't know for sure that the existing event came from the originator - it could be spoofed
                # by the originator to have the same UID as the existing event which it would overwrite. Instead what we will do is rename and
                # change the UID of the original event to preserve it and let the user resolve the "duplicate" conflict that results.
                if not existing_organizer:
                    # Copy the raw (including per-user) data of the original, change its UID and write out a new resource
                    changed_calendar = (yield self.recipient_calendar_resource.component()).duplicate()
                    changed_calendar.replacePropertyInAllComponents(Property("UID", str(uuid.uuid4())))
                    name = "%s-%s.ics" % (hashlib.md5(changed_calendar.resourceUID()).hexdigest(), str(uuid.uuid4())[:8],)
                    yield self.recipient_calendar_resource.parentCollection()._createCalendarObjectWithNameInternal(name, changed_calendar, ComponentUpdateState.RAW)

                    # Delete the original resource
                    yield self.deleteCalendarResource(self.recipient_calendar_resource)

                    # Reset state to make it look like a new iTIP being processed
                    self.recipient_calendar = None
                    self.recipient_calendar_resource = None
                    self.new_resource = True

        # Handle splitting of data early so we can preserve per-attendee data
        if self.message.hasProperty("X-CALENDARSERVER-SPLIT-OLDER-UID"):
            if config.Scheduling.Options.Splitting.Enabled:
                # Tell the existing resource to split
                log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' splitting UID: '{uid}'", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
                split = (yield self.doImplicitAttendeeSplit())
                if split:
                    returnValue((True, False, False, None,))
            else:
                self.message.removeProperty("X-CALENDARSERVER-SPLIT-OLDER-UID")
                self.message.removeProperty("X-CALENDARSERVER-SPLIT-RID")

        elif self.message.hasProperty("X-CALENDARSERVER-SPLIT-NEWER-UID"):
            if config.Scheduling.Options.Splitting.Enabled:
                log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' ignoring UID: '{uid}' - split already done", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
                returnValue((True, False, False, None,))
            else:
                self.message.removeProperty("X-CALENDARSERVER-SPLIT-NEWER-UID")
                self.message.removeProperty("X-CALENDARSERVER-SPLIT-RID")

        # Different based on method
        if self.method == "REQUEST":
            result = (yield self.doImplicitAttendeeRequest())
        elif self.method == "CANCEL":
            result = (yield self.doImplicitAttendeeCancel())
        elif self.method == "ADD":
            # TODO: implement ADD
            result = (False, False, False, None)
        elif self.method == "POLLSTATUS":
            result = (yield self.doImplicitAttendeePollStatus())
        else:
            # NB We should never get here as we will have rejected unsupported METHODs earlier.
            result = (True, True, False, None,)

        returnValue(result)

    @inlineCallbacks
    def doImplicitAttendeeSplit(self):
        """
        Handle splitting of the existing calendar data.
        """
        olderUID = self.message.propertyValue("X-CALENDARSERVER-SPLIT-OLDER-UID")
        split_rid = self.message.propertyValue("X-CALENDARSERVER-SPLIT-RID")
        if olderUID is None or split_rid is None:
            returnValue(False)

        # Split the resource
        yield self.recipient_calendar_resource.splitForAttendee(rid=split_rid, olderUID=olderUID)

        returnValue(True)

    @inlineCallbacks
    def doImplicitAttendeeRequest(self):
        """
        An iTIP REQUEST message has been sent to an attendee. If there is no existing resource, we will simply
        create a new one. If there is an existing resource we need to reconcile the changes between it and the
        iTIP message.

        @return: C{tuple} of (processed, auto-processed, store inbox item, changes)
        """

        # Check if the incoming data has the recipient declined in all instances.
        attendees = self.message.getAttendeeProperties((self.recipient.cuaddr,))
        all_declined = all([attendee.parameterValue("PARTSTAT", "NEEDS-ACTION") == "DECLINED" for attendee in attendees])

        # If we have a recipient item in the trash, and the incoming message has at least one undeclined partstat, then remove the trash
        # item right now so that we treat the iTIP message as a new invite.
        if not self.new_resource and self.recipient_in_trash and not all_declined:
            yield self.deleteCalendarResource(self.recipient_calendar_resource)
            # Reset state to make it look like a new iTIP being processed
            self.recipient_calendar = None
            self.recipient_calendar_resource = None
            self.recipient_in_trash = False
            self.new_resource = True

        # If there is no existing copy, then look for default calendar and copy it here
        if self.new_resource:

            # Check if the incoming data has the recipient declined in all instances. In that case we will not create
            # a new resource as chances are the recipient previously deleted the resource and we want to keep it deleted.
            if all_declined:
                log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' processing METHOD:REQUEST, UID: '{uid}' - ignoring all declined", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
                returnValue((True, False, False, None,))

            # Check for default calendar
            default = (yield self.recipient.inbox.viewerHome().defaultCalendar(self.message.mainType()))
            if default is None:
                log.error("No default calendar for recipient: '{recip}'.", recip=self.recipient.cuaddr)
                raise ImplicitProcessorException(iTIPRequestStatus.NO_USER_SUPPORT)

            log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' processing METHOD:REQUEST, UID: '{uid}' - new processed", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
            new_calendar = iTipProcessing.processNewRequest(self.message, self.recipient.cuaddr, creating=True)

            # Handle auto-reply behavior
            organizer = normalizeCUAddr(self.message.getOrganizer())
            if (yield self.recipient.record.canAutoSchedule(organizer=organizer)):
                # auto schedule mode can depend on who the organizer is
                mode = yield self.recipient.record.getAutoScheduleMode(organizer=organizer)
                send_reply, store_inbox, partstat, accounting = (yield self.checkAttendeeAutoReply(new_calendar, mode))
                if accounting is not None:
                    accounting["action"] = "create"
                    emitAccounting(
                        "AutoScheduling",
                        self.recipient.record,
                        json.dumps(accounting) + "\r\n",
                        filename=self.uid.encode("base64")[:-1] + ".txt"
                    )

                # Only store inbox item when reply is not sent or always for users
                store_inbox = store_inbox or self.recipient.record.getCUType() == "INDIVIDUAL"
            else:
                send_reply = False
                store_inbox = True

            new_resource = (yield self.writeCalendarResource(default, None, new_calendar))

            if send_reply:
                # Track outstanding auto-reply processing
                log.debug("ImplicitProcessing - recipient '{recip}' processing UID: '{uid}' - auto-reply queued", recip=self.recipient.cuaddr, uid=self.uid)
                yield ScheduleAutoReplyWork.autoReply(self.txn, new_resource, partstat)

            # Build the schedule-changes XML element
            changes = customxml.ScheduleChanges(
                customxml.DTStamp(),
                customxml.Action(
                    customxml.Create(),
                ),
            )
            result = (True, send_reply, store_inbox, changes,)
        else:
            # Processing update to existing event
            new_calendar, rids = iTipProcessing.processRequest(self.message, self.recipient_calendar, self.recipient.cuaddr)
            if new_calendar:

                # Handle auto-reply behavior
                organizer = normalizeCUAddr(self.message.getOrganizer())
                if (yield self.recipient.record.canAutoSchedule(organizer=organizer)) and not hasattr(self.txn, "doing_attendee_refresh"):
                    # auto schedule mode can depend on who the organizer is
                    mode = yield self.recipient.record.getAutoScheduleMode(organizer=organizer)
                    send_reply, store_inbox, partstat, accounting = (yield self.checkAttendeeAutoReply(new_calendar, mode))
                    if accounting is not None:
                        accounting["action"] = "modify"
                        emitAccounting(
                            "AutoScheduling",
                            self.recipient.record,
                            json.dumps(accounting) + "\r\n",
                            filename=self.uid.encode("base64")[:-1] + ".txt"
                        )

                    # Only store inbox item when reply is not sent or always for users
                    store_inbox = store_inbox or self.recipient.record.getCUType() == "INDIVIDUAL"
                else:
                    # Do not store inbox item if the resource is remaining in the trash
                    send_reply = False
                    store_inbox = not self.recipient_in_trash

                # Let the store know that no time-range info has changed for a refresh (assuming that
                # no auto-accept changes were made)
                if hasattr(self.txn, "doing_attendee_refresh"):
                    new_calendar.noInstanceIndexing = not send_reply

                # Update the attendee's copy of the event
                log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' processing METHOD:REQUEST, UID: '{uid}' - updating event", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)

                new_resource = (yield self.writeCalendarResource(None, self.recipient_calendar_resource, new_calendar))

                if send_reply:
                    # Track outstanding auto-reply processing
                    log.debug("ImplicitProcessing - recipient '{recip}' processing UID: '{uid}' - auto-reply queued", recip=self.recipient.cuaddr, uid=self.uid)
                    yield ScheduleAutoReplyWork.autoReply(self.txn, new_resource, partstat)

                # Build the schedule-changes XML element
                update_details = []
                for rid, props_changed in sorted(rids.iteritems(), key=lambda x: x[0]):

                    # We do not report removals as those will already have been processed via a CANCEL
                    if props_changed is None:
                        continue

                    recurrence = []
                    if rid is None:
                        recurrence.append(customxml.Master())
                    else:
                        recurrence.append(customxml.RecurrenceID.fromString(rid.getText()))
                    changes = []
                    for propName, paramNames in sorted(props_changed.iteritems(), key=lambda x: x[0]):
                        params = tuple([customxml.ChangedParameter(name=param) for param in paramNames])
                        changes.append(customxml.ChangedProperty(*params, **{"name": propName}))
                    recurrence.append(customxml.Changes(*changes))
                    update_details += (customxml.Recurrence(*recurrence),)

                changes = customxml.ScheduleChanges(
                    customxml.DTStamp(),
                    customxml.Action(
                        customxml.Update(*update_details),
                    ),
                )

                # Refresh from another Attendee should not have Inbox item
                if hasattr(self.txn, "doing_attendee_refresh"):
                    store_inbox = False

                result = (True, send_reply, store_inbox, changes,)

            else:
                # Request needs to be ignored
                log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' processing METHOD:REQUEST, UID: '{uid}' - ignoring", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
                result = (True, False, False, None,)

        returnValue(result)

    @inlineCallbacks
    def doImplicitAttendeeCancel(self):
        """
        An iTIP CANCEL message has been sent to an attendee. If there is no existing resource, we will simply
        ignore the message. If there is an existing resource we need to reconcile the changes between it and the
        iTIP message.

        @return: C{tuple} of (processed, auto-processed, store inbox item, changes)
        """

        # Note that we never get here if there is no existing copy, i.e., self.new_resource is True, because that case is handled directly in
        # self.doImplicitAttendee().

        # If there attendee copy is in the trash, then we should remove the trash item and not store an inbox item as there is no reason to recover
        # the cancelled meeting.
        if self.recipient_in_trash:
            # Note we should never get here as this test is
            log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' ignoring METHOD:CANCEL, UID: '{uid}' - attendee copy in trash", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
            yield self.deleteCalendarResource(self.recipient_calendar_resource)
            result = (True, True, False, None)
        else:
            # Need to check for auto-respond attendees. These need to suppress the inbox message
            # if the cancel is processed. However, if the principal is a user we always force the
            # inbox item on them even if auto-schedule is true so that they get a notification
            # of the cancel.
            organizer = normalizeCUAddr(self.message.getOrganizer())
            autoprocessed = yield self.recipient.record.canAutoSchedule(organizer=organizer)
            store_inbox = not autoprocessed or self.recipient.record.getCUType() == "INDIVIDUAL"

            # Check to see if this is a cancel of the entire event
            processed_message, delete_original, rids = iTipProcessing.processCancel(self.message, self.recipient_calendar, autoprocessing=autoprocessed)
            if processed_message:
                if autoprocessed and accountingEnabled("AutoScheduling", self.recipient.record):
                    accounting = {
                        "action": "cancel",
                        "when": DateTime.getNowUTC().getText(),
                        "deleting": delete_original,
                    }
                    emitAccounting(
                        "AutoScheduling",
                        self.recipient.record,
                        json.dumps(accounting) + "\r\n",
                        filename=self.uid.encode("base64")[:-1] + ".txt"
                    )

                if delete_original:

                    # Delete the attendee's copy of the event
                    log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' processing METHOD:CANCEL, UID: '{uid}' - deleting entire event", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
                    yield self.deleteCalendarResource(self.recipient_calendar_resource)

                    # Build the schedule-changes XML element
                    changes = customxml.ScheduleChanges(
                        customxml.DTStamp(),
                        customxml.Action(
                            customxml.Cancel(),
                        ),
                    )
                    result = (True, autoprocessed, store_inbox, changes,)

                else:

                    # Update the attendee's copy of the event
                    log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' processing METHOD:CANCEL, UID: '{uid}' - updating event", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
                    yield self.writeCalendarResource(None, self.recipient_calendar_resource, self.recipient_calendar)

                    # Build the schedule-changes XML element
                    if rids:
                        action = customxml.Cancel(
                            *[customxml.Recurrence(customxml.RecurrenceID.fromString(rid.getText())) for rid in sorted(rids)]
                        )
                    else:
                        action = customxml.Cancel()
                    changes = customxml.ScheduleChanges(
                        customxml.DTStamp(),
                        customxml.Action(action),
                    )
                    result = (True, autoprocessed, store_inbox, changes)
            else:
                log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' processing METHOD:CANCEL, UID: '{uid}' - ignoring", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
                result = (True, False, False, None)

        returnValue(result)

    @inlineCallbacks
    def doImplicitAttendeePollStatus(self):
        """
        An iTIP message status update has been sent to an attendee by the organizer. We need to update the
        attendee state based on the nature of the iTIP message.
        """
        # If there is no existing copy, then we must fail
        if self.new_resource:
            log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' processing METHOD:POLLSTATUS, UID: '{uid}' - attendee has no copy", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
            returnValue((True, False, False, None,))

        processed_message = iTipProcessing.processPollStatus(self.message, self.recipient_calendar)

        # Let the store know that no time-range info has changed for a refresh (assuming that
        # no auto-accept changes were made)
        processed_message.noInstanceIndexing = True

        # Update the attendee's copy of the event
        log.debug("ImplicitProcessing - originator '{orig}' to recipient '{recip}' processing METHOD:POLLSTATUS, UID: '{uid}' - updating poll", orig=self.originator.cuaddr, recip=self.recipient.cuaddr, uid=self.uid)
        yield self.writeCalendarResource(None, self.recipient_calendar_resource, processed_message)

        returnValue((True, False, False, None,))

    @inlineCallbacks
    def checkAttendeeAutoReply(self, calendar, automode):
        """
        Check whether a reply to the given iTIP message is needed and if so make the
        appropriate changes to the calendar data. Changes are only made for the case
        where the PARTSTAT of the attendee is NEEDS-ACTION - i.e., any existing state
        is left unchanged. This allows, e.g., proxies to decline events that would
        otherwise have been auto-accepted and those stay declined as non-schedule-change
        updates are received.

        @param calendar: the iTIP message to process
        @type calendar: L{Component}
        @param automode: the auto-schedule mode for the recipient
        @type automode: L{txdav.who.idirectory.AutoScheduleMode}

        @return: C{tuple} of C{bool}, C{bool}, C{str} indicating whether changes were made, whether the inbox item
            should be added, and the new PARTSTAT.
        """
        if accountingEnabled("AutoScheduling", self.recipient.record):
            accounting = {
                "when": DateTime.getNowUTC().getText(),
                "automode": automode.name,
                "changed": False,
            }
        else:
            accounting = None

        # First ignore the none mode
        if automode == AutoScheduleMode.none:
            returnValue((False, True, "", accounting,))
        elif not automode:
            automode = {
                "none": AutoScheduleMode.none,
                "accept-always": AutoScheduleMode.accept,
                "decline-always": AutoScheduleMode.decline,
                "accept-if-free": AutoScheduleMode.acceptIfFree,
                "decline-if-busy": AutoScheduleMode.declineIfBusy,
                "automatic": AutoScheduleMode.acceptIfFreeDeclineIfBusy,
            }.get(
                config.Scheduling.Options.AutoSchedule.DefaultMode,
                AutoScheduleMode.acceptIfFreeDeclineIfBusy
            )

        log.debug("ImplicitProcessing - recipient '{recip}' processing UID: '{uid}' - checking for auto-reply with mode: {mode}", recip=self.recipient.cuaddr, uid=self.uid, mode=automode.name)
        cuas = self.recipient.record.calendarUserAddresses

        # First expand current one to get instances (only go 1 year into the future)
        default_future_expansion_duration = Duration(days=config.Scheduling.Options.AutoSchedule.FutureFreeBusyDays)
        expand_max = DateTime.getToday() + default_future_expansion_duration
        instances = calendar.expandTimeRanges(expand_max, ignoreInvalidInstances=True)

        if accounting is not None:
            accounting["expand-max"] = expand_max.getText()
            accounting["instances"] = len(instances.instances)

        # We are going to ignore auto-accept processing for anything more than a day old (actually use -2 days
        # to add some slop to account for possible timezone offsets)
        min_date = DateTime.getToday()
        min_date.offsetDay(-2)
        allOld = True

        # Cache the current attendee partstat on the instance object for later use, and
        # also mark whether the instance time slot would be free
        for instance in instances.instances.itervalues():
            attendee = instance.component.getAttendeeProperty(cuas)
            instance.partstat = attendee.parameterValue("PARTSTAT", "NEEDS-ACTION") if attendee else None
            instance.free = True
            instance.active = (instance.end > min_date)
            if instance.active:
                allOld = False

        instances = sorted(instances.instances.values(), key=lambda x: x.rid)

        # If every instance is in the past we punt right here so we don't waste time on freebusy lookups etc.
        # There will be no auto-accept and no inbox item stored (so as not to waste storage on items that will
        # never be processed).
        if allOld:
            if accounting is not None:
                accounting["status"] = "all instances are old"
            returnValue((False, False, "", accounting,))

        # Extract UID from primary component as we want to ignore this one if we match it
        # in any calendars.
        uid = calendar.resourceUID()

        # Object to do freebusy query
        freebusy = FreebusyQuery(recipient=self.recipient, excludeUID=uid, accountingItems=accounting if len(instances) == 1 else None)

        # Now compare each instance time-range with the index and see if there is an overlap
        fbset = (yield self.recipient.inbox.ownerHome().loadCalendars())
        fbset = [fbcalendar for fbcalendar in fbset if fbcalendar.isUsedForFreeBusy()]
        if accounting is not None:
            accounting["fbset"] = [testcal.name() for testcal in fbset]
            accounting["tr"] = []

        for testcal in fbset:

            # Get the timezone property from the collection, and store in the query filter
            # for use during the query itself.
            tz = testcal.getTimezone()
            tzinfo = tz.gettimezone() if tz is not None else Timezone.UTCTimezone

            # Now do search for overlapping time-range and set instance.free based
            # on whether there is an overlap or not.
            # NB Do this in reverse order so that the date farthest in the future is tested first - that will
            # ensure that freebusy that far into the future is determined and will trigger time-range caching
            # and indexing out that far - and that will happen only once through this loop.
            for instance in reversed(instances):
                if instance.partstat == "NEEDS-ACTION" and instance.free and instance.active:
                    try:
                        # First list is BUSY, second BUSY-TENTATIVE, third BUSY-UNAVAILABLE
                        fbinfo = FreebusyQuery.FBInfo([], [], [])

                        def makeTimedUTC(dt):
                            dt = dt.duplicate()
                            if dt.isDateOnly():
                                dt.setDateOnly(False)
                                dt.setHHMMSS(0, 0, 0)
                            if dt.floating():
                                dt.setTimezone(tzinfo)
                                dt.adjustToUTC()
                            return dt

                        tr = Period(
                            makeTimedUTC(instance.start),
                            makeTimedUTC(instance.end),
                        )

                        freebusy.timerange = tr
                        yield freebusy.generateFreeBusyInfo([testcal, ], fbinfo)

                        # If any fbinfo entries exist we have an overlap
                        if len(fbinfo.busy) or len(fbinfo.tentative) or len(fbinfo.unavailable):
                            instance.free = False
                        if accounting is not None:
                            accounting["tr"].insert(0, (tr.getStart().getText(), tr.getEnd().getText(), instance.free,))
                    except QueryMaxResources:
                        instance.free[instance] = False
                        log.info("Exceeded number of matches whilst trying to find free-time.")
                        if accounting is not None:
                            accounting["problem"] = "Exceeded number of matches"

            # If everything is declined we can exit now
            if not any([instance.free for instance in instances]):
                break

        if accounting is not None:
            accounting["tr"] = accounting["tr"][:30]

        # Now adjust the instance.partstat currently set to "NEEDS-ACTION" to the
        # value determined by auto-accept logic based on instance.free state. However,
        # ignore any instance in the past - leave them as NEEDS-ACTION.
        partstat_counts = collections.defaultdict(int)
        for instance in instances:
            if instance.partstat == "NEEDS-ACTION" and instance.active:
                if automode == AutoScheduleMode.accept:
                    freePartstat = busyPartstat = "ACCEPTED"
                elif automode == AutoScheduleMode.decline:
                    freePartstat = busyPartstat = "DECLINED"
                else:
                    freePartstat = "ACCEPTED" if automode in (
                        AutoScheduleMode.acceptIfFree,
                        AutoScheduleMode.acceptIfFreeDeclineIfBusy,
                    ) else "NEEDS-ACTION"
                    busyPartstat = "DECLINED" if automode in (
                        AutoScheduleMode.declineIfBusy,
                        AutoScheduleMode.acceptIfFreeDeclineIfBusy,
                    ) else "NEEDS-ACTION"
                instance.partstat = freePartstat if instance.free else busyPartstat
            partstat_counts[instance.partstat] += 1

        if len(partstat_counts) == 0:
            # Nothing to do
            if accounting is not None:
                accounting["status"] = "no partstat changes"
            returnValue((False, False, "", accounting,))

        elif len(partstat_counts) == 1:
            # Do the simple case of all PARTSTATs the same separately
            # Extract the ATTENDEE property matching current recipient from the calendar data
            attendeeProps = calendar.getAttendeeProperties(cuas)
            if not attendeeProps:
                if accounting is not None:
                    accounting["status"] = "no attendee to change"
                returnValue((False, False, "", accounting,))

            made_changes = False
            partstat = partstat_counts.keys()[0]
            for component in calendar.subcomponents():
                made_changes |= self.resetAttendeePartstat(component, cuas, partstat)
            store_inbox = partstat == "NEEDS-ACTION"

            if accounting is not None:
                accounting["status"] = "setting all partstats to {}".format(partstat) if made_changes else "all partstats correct"
                accounting["changed"] = made_changes

        else:
            # Hard case: some accepted, some declined, some needs-action
            # What we will do is mark any master instance as accepted, then mark each existing
            # overridden instance as accepted or declined, and generate new overridden instances for
            # any other declines.

            made_changes = False
            store_inbox = False
            partstat = "MIXED RESPONSE"

            # Default state is whichever of free or busy has most instances
            defaultPartStat = max(sorted(partstat_counts.items()), key=lambda x: x[1])[0]

            # See if there is a master component first
            hadMasterRsvp = False
            master = calendar.masterComponent()
            if master:
                attendee = master.getAttendeeProperty(cuas)
                if attendee:
                    hadMasterRsvp = attendee.parameterValue("RSVP", "FALSE") == "TRUE"
                    if defaultPartStat == "NEEDS-ACTION":
                        store_inbox = True
                    made_changes |= self.resetAttendeePartstat(master, cuas, defaultPartStat)

            # Look at expanded instances and change partstat accordingly
            for instance in instances:

                overridden = calendar.overriddenComponent(instance.rid)
                if not overridden and instance.partstat == defaultPartStat:
                    # Nothing to do as state matches the master
                    continue

                if overridden:
                    # Change ATTENDEE property to match new state
                    if instance.partstat == "NEEDS-ACTION" and instance.active:
                        store_inbox = True
                    made_changes |= self.resetAttendeePartstat(overridden, cuas, instance.partstat)
                else:
                    # Derive a new overridden component and change partstat. We also need to make sure we restore any RSVP
                    # value that may have been overwritten by any change to the master itself.
                    derived = calendar.deriveInstance(instance.rid)
                    if derived is not None:
                        attendee = derived.getAttendeeProperty(cuas)
                        if attendee:
                            if instance.partstat == "NEEDS-ACTION" and instance.active:
                                store_inbox = True
                            self.resetAttendeePartstat(derived, cuas, instance.partstat, hadMasterRsvp)
                            made_changes = True
                            calendar.addComponent(derived)

            if accounting is not None:
                accounting["status"] = "mixed partstat changes" if made_changes else "mixed partstats correct"
                accounting["changed"] = made_changes

        # Fake a SCHEDULE-STATUS on the ORGANIZER property
        if made_changes:
            calendar.setParameterToValueForPropertyWithValue("SCHEDULE-STATUS", iTIPRequestStatus.MESSAGE_DELIVERED_CODE, "ORGANIZER", None)

        returnValue((made_changes, store_inbox, partstat, accounting,))

    @inlineCallbacks
    def writeCalendarResource(self, collection, resource, calendar):
        """
        Write out the calendar resource (iTIP) message to the specified calendar, either over-writing the named
        resource or by creating a new one.

        @param collection: the calendar collection to store the resource in.
        @type: L{Calendar}
        @param resource: the resource object to write to, or C{None} to write a new resource.
        @type: L{CalendarObject}
        @param calendar: the calendar data to write.
        @type: L{Component}

        @return: the object resource written to (either the one passed in or a new one)
        @rtype: L{CalendarObject}
        """

        # Create a new name if one was not provided
        internal_state = ComponentUpdateState.ORGANIZER_ITIP_UPDATE if self.isOrganizerReceivingMessage() else ComponentUpdateState.ATTENDEE_ITIP_UPDATE
        if resource is None:
            name = "%s-%s.ics" % (hashlib.md5(calendar.resourceUID()).hexdigest(), str(uuid.uuid4())[:8],)
            newchild = (yield collection._createCalendarObjectWithNameInternal(name, calendar, internal_state))
        else:
            yield resource._setComponentInternal(calendar, internal_state=internal_state)
            newchild = resource

        returnValue(newchild)

    @inlineCallbacks
    def deleteCalendarResource(self, resource):
        """
        Delete the calendar resource in the specified calendar.

        @param collURL: the URL of the calendar collection.
        @type name: C{str}
        @param collection: the calendar collection to delete the resource from.
        @type collection: L{CalDAVResource}
        @param name: the resource name to write into, or {None} to write a new resource.
        @type name: C{str}
        """

        yield resource._removeInternal(internal_state=ComponentRemoveState.INTERNAL, useTrash=False)

    def resetAttendeePartstat(self, component, cuas, partstat, hadRSVP=False):
        """
        Change the PARTSTAT on any ATTENDEE properties that match the list of calendar user
        addresses on the component passed in. Also adjust the TRANSP property to match the
        new PARTSTAT value.

        @param component: an iCalendar component to modify
        @type attendees: L{Component}
        @param cuas: a list of calendar user addresses to match
        @type attendees: C{list} or C{tuple}
        @param partstat: new PARTSTAT to set
        @type partstat: C{str}
        @param hadRSVP: indicates whether RSVP should be added when changing to NEEDS-ACTION
        @type hadRSVP: C{bool}

        @return: C{True} if any change was made, C{False} otherwise
        """

        madeChanges = False
        attendee = component.getAttendeeProperty(cuas)
        if attendee:
            if attendee.parameterValue("PARTSTAT", "NEEDS-ACTION") != partstat:
                attendee.setParameter("PARTSTAT", partstat)
                madeChanges = True

            # Always remove RSVP when a state other than NEEDS-ACTION is set - this
            # is only an attendee change so madeChanges does not need to be changed
            try:
                if attendee.parameterValue("PARTSTAT", "NEEDS-ACTION") != "NEEDS-ACTION":
                    attendee.removeParameter("RSVP")
                elif hadRSVP:
                    attendee.setParameter("RSVP", "TRUE")
            except KeyError:
                pass

            # Adjust TRANSP to OPAQUE if PARTSTAT is ACCEPTED, otherwise TRANSPARENT
            component.replaceProperty(Property("TRANSP", "OPAQUE" if partstat == "ACCEPTED" else "TRANSPARENT"))

            if madeChanges:
                attendee.setParameter("X-CALENDARSERVER-AUTO", DateTime.getNowUTC().getText())
                attendee.removeParameter(DTSTAMP_PARAM)

        return madeChanges

    @inlineCallbacks
    def doImplicitAttendeeEventFix(self, ex):

        # Only certain types of exception should be handled - ones related to calendar data errors.
        # All others should result in the scheduling response coming back as a 5.x code

        if type(ex) not in (InvalidOverriddenInstanceError, HTTPError):
            raise ImplicitProcessorException("5.1;Service unavailable")

        # Check to see whether the originator is hosted on this server
        if not self.originator.record:
            raise ImplicitProcessorException("5.1;Service unavailable")

        # Locate the originator's copy of the event
        calendar_resource = (yield getCalendarObjectForRecord(self.txn, self.originator.record, self.uid))
        if calendar_resource is None:
            raise ImplicitProcessorException("5.1;Service unavailable")
        originator_calendar = (yield calendar_resource.componentForUser(self.originator.record.uid))

        # Get attendee's view of that
        originator_calendar = originator_calendar.duplicate()
        iTipGenerator.generateAttendeeView(originator_calendar, (self.recipient.cuaddr,), None)

        # Locate the attendee's copy of the event if it exists.
        recipient_resource = (yield getCalendarObjectForRecord(self.txn, self.recipient.record, self.uid))

        # We only need to fix data that already exists
        if recipient_resource is not None:
            if originator_calendar.mainType() is not None:
                yield self.writeCalendarResource(None, recipient_resource, originator_calendar)
            else:
                yield self.deleteCalendarResource(recipient_resource)

        returnValue(True)
