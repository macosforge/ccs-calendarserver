Developer's Guide to Contributing to the Calendar Server
========================================================

If you are interested in contributing to the Calendar and Contacts
Server project, please read this document.


Participating in the Community
==============================

The Calendar and Contacts Server began in 1996 -- an open source
project sponsored and hosted by Apple Inc. (http://www.apple.com/).
The project is now hosted at GitHub, and although it lives within
the "apple" namespace it's still a true open-source project under
an Apache license.  Contributions from other developers are welcome,
and, as with all open development projects, may lead to "commit
access" and a voice in the future of the project.

The community exists mainly through mailing lists and a GitHub
repository. To participate, go to:

  https://www.calendarserver.org/MailingLists.html

and join the appropriate mailing lists.  We also use IRC, as described
here:

  https://www.calendarserver.org/IRC.html

There are many ways to join the project.  One may write code, test the
software and file bugs, write documentation, etc.

The issue tracking database is here:

  https://github.com/apple/ccs-calendarserver/issues

To help manage the issues database, read over the issue summaries,
looking and testing for issues that are either invalid, or are
duplicates of other issues. Both kinds are very common, the first
because bugs often get unknowingly fixed as side effects of other
changes in the code, and the second because people sometimes file an
issue without noticing that it has already been reported. If you are
not sure about an issue, post a question to

  calendarserver-dev@lists.macosforge.org.

Before filing bugs, please take a moment to perform a quick search to
see if someone else has already filed your bug.  In that case, add a
comment to the existing bug if appropriate and monitor it, rather than
filing a duplicate.


Obtaining the Code
==================

The source code to the Calendar and Contacts Server is available via
Git at this repository URL:

  https://github.com/apple/ccs-calendarserver.git


Directory Layout
================

A rough guide to the source tree:

 * ``doc/`` - User and developer documentation, including relevant
   protocol specifications and extensions.

 * ``bin/`` - Executable programs.

 * ``conf/`` - Configuration files.

 * ``calendarserver/`` - Source code for the Calendar and Contacts
   Server

 * ``twistedcaldav/`` - Source code for CalDAV library

 * ``twisted/`` - Files required to set up the Calendar and Contacts
   Server as a Twisted service.  Twisted (http://twistedmatrix.com/)
   is a networking framework upon which the Calendar and Contacts
   Server is built.

 * ``locales/`` - Localization files.

 * ``contrib/`` - Extra stuff that works with the Calendar and
   Contacts Server, or that helps integrate with other software
   (including operating systems), but that the Calendar and Contacts
   Server does not depend on.

 * ``support/`` - Support files of possible use to developers.


Coding Standards
================

The vast majority of the Calendar and Contacts Server is written in
the Python programming language.  When writing Python code for the
Calendar and Contacts Server, please observe the following
conventions.

Please note that all of our code at present does not follow these
standards, but that does not mean that one shouldn't bother to do so.
On the contrary, code changes that do nothing but reformat code to
comply with these standards are welcome, and code changes that do not
conform to these standards are discouraged.

**We require Python 2.6 or higher.** It therefore is OK to write code
that does not work with Python versions older than 2.6.

Read PEP-8:

  https://www.python.org/dev/peps/pep-0008/

For the most part, our code should follow PEP-8, with a few exceptions
and a few additions.  It is also useful to review the Twisted Coding
Standard, from which we borrow some standards, though we don't
strictly follow it:

   https://twistedmatrix.com/documents/current/core/development/policy/coding-standard.html

Key items to follow, and specifics:

 * Indent level is 4 spaces.

 * Never indent code with tabs.  Always use spaces.

PEP-8 items we do not follow:

 * PEP-8 recommends using a backslash to break long lines up:

   ::

     if width == 0 and height == 0 and \
         color == 'red' and emphasis == 'strong' or \
         highlight > 100:
             raise ValueError("sorry, you lose")

   Don't do that, it's gross, and the indentation for the ``raise`` line
   gets confusing.  Use parentheses:

   ::

     if (
         width == 0 and
         height == 0 and
         color == "red" and
         emphasis == "strong" or
         highlight > 100
     ):
         raise ValueError("sorry, you lose")

   Just don't do it the way PEP-8 suggests:

   ::

     if width == 0 and height == 0 and (color == 'red' or
                                        emphasis is None):
         raise ValueError("I don't think so")

   Because that's just silly.

Additions:

 * Close parentheses and brackets such as ``()``, ``[]`` and ``{}`` at the
   same indent level as the line in which you opened it:

   ::

     launchAtTarget(
         target="David",
         object=PaperWad(
             message="Yo!",
             crumpleFactor=0.7,
         ),
         speed=0.4,
     )

 * Long lines are often due to long strings.  Try to break strings up
   into multiple lines:

   ::

     processString(
        "This is a very long string with a lot of text. "
        "Fortunately, it is easy to break it up into parts "
        "like this."
     )

   Similarly, callables that take many arguments can be broken up into
   multiple lines, as in the ``launchAtTarget()`` example above.

 * Breaking generator expressions and list comprehensions into
   multiple lines can improve readability.  For example:

   ::

     myStuff = (
         item.obtainUsefulValue()
         for item in someDataStore
         if item.owner() == me
     )

 * Import symbols (especially class names) from modules instead of
   importing modules and referencing the symbol via the module unless
   it doesn't make sense to do so.  For example:

   ::

     from subprocess import Popen

     process = Popen(...)

   Instead of:

   ::

     import subprocess

     process = subprocess.Popen(...)

   This makes code shorter and makes it easier to replace one implementation
   with another.

 * All files should have an ``__all__`` specification.  Put them at the
   top of the file, before imports (PEP-8 puts them at the top, but
   after the imports), so you can see what the public symbols are for
   a file right at the top.

 * It is more important that symbol names are meaningful than it is
   that they be concise.  ``x`` is rarely an appropriate name for a
   variable.  Avoid contractions: ``transmogrifierStatus`` is more useful
   to the reader than ``trmgStat``.

 * A deferred that will be immediately returned may be called ``d``:

   ::

     d = doThisAndThat()
     d.addCallback(onResult)
     d.addErrback(onError)
     return d

 * Do not use ``deferredGenerator``.  Use ``inlineCallbacks`` instead.

 * That said, avoid using ``inlineCallbacks`` when chaining deferreds
   is straightforward, as they are more expensive.  Use
   ``inlineCallbacks`` when necessary for keeping code maintainable,
   such as when creating serialized deferreds in a for loop.

 * ``_`` may be used to denote unused callback arguments:

   ::

     def onCompletion(_):
       # Don't care about result of doThisAndThat() in here;
       # we only care that it has completed.
       doNextThing()

     d = doThisAndThat()
     d.addCallback(onCompletion)
     return d

 * Do not prefix symbols with ``_`` unless they might otherwise be
   exposed as a public symbol: a private method name should begin with
   ``_``, but a locally scoped variable should not, as there is no
   danger of it being exposed. Locally scoped variables are already
   private.

 * Per twisted convention, use camel-case (``fuzzyWidget``,
   ``doThisAndThat()``) for symbol names instead of using underscores
   (``fuzzy_widget``, ``do_this_and_that()``).

   Use of underscores is reserved for implied dispatching and the like
   (eg. ``http_FOO()``).  See the Twisted Coding Standard for details.

 * Do not use ``%``-formatting:

   ::

     error = "Unexpected value: %s" % (value,)

   Use PEP-3101 formatting instead:

   ::

     error = "Unexpected value: {value}".format(value=value)

 * If you must use ``%``-formatting for some reason, always use a tuple as
   the format argument, even when only one value is being provided:

   ::

     error = "Unexpected value: %s" % (value,)

   Never use the non-tuple form:

   ::

     error = "Unexpected value: %s" % value

   Which is allowed in Python, but results in a programming error if
   ``type(value) is tuple and len(value) != 1``.

 * Don't use a trailing ``,`` at the end of a tuple if it's on one line:

   ::

     numbers = (1,2,3,) # No
     numbers = (1,2,3)  # Yes

   The trailing comma is desirable on multiple lines, though, as that makes
   re-ordering items easy, and avoids a diff on the last line when adding
   another:

   ::

     strings = (
       "This is a string.",
       "And so is this one.",
       "And here is yet another string.",
     )

 * Docstrings are important.  All public symbols (anything declared in
   ``__all__``) must have a correct docstring.  The script
   ``docs/Developer/gendocs`` will generate the API documentation using
   ``pydoctor``.  See the ``pydoctor`` documentation for details on the
   formatting:

     http://codespeak.net/~mwh/pydoctor/

   Note: existing docstrings need a complete review.

 * Use PEP-257 as a guideline for docstrings.

 * Begin all multi-line docstrings with 3 double quotes and a
   newline:

   ::

     def doThisAndThat(...):
       """
       Do this, and that.
       ...
       """


Best Practices
==============

 * If a callable is going to return a Deferred some of the time, it
   should return a deferred all of the time.  Return ``succeed(value)``
   instead of ``value`` if necessary.  This avoids forcing the caller
   to check as to whether the value is a deferred or not (eg. by using
   ``maybeDeferred()``), which is both annoying to code and potentially
   expensive at runtime.

 * Be proactive about closing files and file-like objects.

   For a lot of Python software, letting Python close the stream for
   you works fine, but in a long-lived server that's processing many
   data streams at a time, it is important to close them as soon as
   possible.

   On some platforms (eg. Windows), deleting a file will fail if the
   file is still open.  By leaving it up to Python to decide when to
   close a file, you may find yourself being unable to reliably delete
   it.

   The most reliable way to ensure that a stream is closed is to put
   the call to ``close()`` in a ``finally`` block:

   ::

     stream = file(somePath)
     try:
       ... do something with stream ...
     finally:
       stream.close()


Testing
=======

Be sure that all of the units tests pass before you commit new code.
Code that breaks units tests may be reverted without further
discussion; it is up to the committer to fix the problem and try
again.

Note that repeatedly committing code that breaks units tests presents
a possible time sink for other developers, and is not looked upon
favorably.

Units tests can be run rather easily by executing the ``./bin/test`` script
at the top of the Calendar and Contacts Server source tree.  By
default, it will run all of the Calendar and Contacts Server tests
followed by all of the Twisted tests.  You can run specific tests by
specifying them as arguments like this:

   ::

    ./bin/test twistedcaldav.static

All non-trivial public callables must have unit tests.  (Note we don't
don't totally comply with this rule; that's a problem we'd like to
fix.)  All other callables should have unit tests.

Units tests are written using the ``twisted.trial`` framework.  Test
module names should start with ``test_``.  Twisted has some tips on
writing tests here:

  https://twistedmatrix.com/documents/current/core/howto/testing.html

  https://twistedmatrix.com/documents/current/core/development/policy/test-standard.html

We also use CalDAVTester (which is a companion to the Calendar and
Contacts Server), which performs more "black box"-type testing against
the server to ensure compliance with the CalDAV protocol.  That requires
running the server with a test configuration and then running
CalDAVTester against it.  Information about CalDAVTester is available here:

  https://github.com/apple/ccs-caldavtester
