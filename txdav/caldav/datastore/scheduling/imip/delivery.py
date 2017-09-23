# -*- test-case-name: txdav.caldav.datastore.scheduling.test.test_imip -*-
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
Handles the sending of scheduling messages via iMIP (mail gateway).
"""

from twext.python.log import Logger
from txweb2 import responsecode
from txweb2.dav.http import ErrorResponse
from txweb2.http import HTTPError
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python.failure import Failure
from twistedcaldav.caldavxml import caldav_namespace
from txdav.caldav.datastore.scheduling.delivery import DeliveryService
from txdav.caldav.datastore.scheduling.imip.outbound import IMIPInvitationWork
from txdav.caldav.datastore.scheduling.itip import iTIPRequestStatus


__all__ = [
    "ScheduleViaIMip",
]

log = Logger()


class ScheduleViaIMip(DeliveryService):

    @classmethod
    def serviceType(cls):
        return DeliveryService.serviceType_imip

    @inlineCallbacks
    def generateSchedulingResponses(self):
        def failForRecipient(recipient):
            err = HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (caldav_namespace, "recipient-failed"),
                "iMIP request failed",
            ))
            self.responses.add(
                recipient.cuaddr,
                Failure(exc_value=err),
                reqstatus=iTIPRequestStatus.SERVICE_UNAVAILABLE,
                suppressErrorLog=True
            )

        # Generate an HTTP client request
        try:
            # We do not do freebusy requests via iMIP
            if self.freebusy:
                raise ValueError("iMIP VFREEBUSY requests not supported.")

            method = self.scheduler.calendar.propertyValue("METHOD")
            if method not in (
                "PUBLISH",
                "REQUEST",
                "REPLY",
                "ADD",
                "CANCEL",
                "DECLINE_COUNTER",
            ):
                log.info(
                    "Could not do server-to-imip method: {method}",
                    method=method,
                )
                for recipient in self.recipients:
                    err = HTTPError(ErrorResponse(
                        responsecode.FORBIDDEN,
                        (caldav_namespace, "recipient-failed"),
                        "iMIP method not allowed: {}".format(method,),
                    ))
                    self.responses.add(
                        recipient.cuaddr,
                        Failure(exc_value=err),
                        reqstatus=iTIPRequestStatus.NO_USER_SUPPORT
                    )
                returnValue(None)

            caldata = str(self.scheduler.calendar)

            for recipient in self.recipients:
                try:
                    toAddr = str(recipient.cuaddr)
                    if not toAddr.lower().startswith("mailto:"):
                        raise ValueError("ATTENDEE address '{}' must be mailto: for iMIP operation.".format(toAddr,))

                    fromAddr = str(self.scheduler.originator.cuaddr)

                    log.debug(
                        "Submitting iMIP message...  To: '{to}', From :'{fr}'\n{data}",
                        to=toAddr,
                        fr=fromAddr,
                        data=caldata,
                    )

                    def enqueueOp(txn):
                        return txn.enqueue(
                            IMIPInvitationWork, fromAddr=fromAddr,
                            toAddr=toAddr, icalendarText=caldata
                        )

                    yield self.scheduler.txn.store().inTransaction(
                        "Submitting iMIP message for UID: {}".format(
                            self.scheduler.calendar.resourceUID(),),
                        enqueueOp
                    )

                except Exception as e:
                    # Generated failed response for this recipient
                    log.debug(
                        "iMIP request {req} failed for recipient {r}: {exc}",
                        req=self,
                        r=recipient,
                        exc=e,
                    )
                    failForRecipient(recipient)

                else:
                    self.responses.add(
                        recipient.cuaddr,
                        responsecode.OK,
                        reqstatus=iTIPRequestStatus.MESSAGE_SENT
                    )

        except Exception as e:
            # Generated failed responses for each recipient
            log.debug("iMIP request {req} failed: {exc}", req=self, exc=e)
            for recipient in self.recipients:
                failForRecipient(recipient)
