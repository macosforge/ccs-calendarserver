##
# Copyright (c) 2015-2017 Apple Inc. All rights reserved.
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
Below are a bunch of useful functions that can be used inside of lldb to debug a Python
process (this applies to cPython only).

The steps are as follows:

    > lldb
    (lldb) target symbols add <path to Python.framework.dSYM>
    (lldb) command script import contrib/tools/lldb_utils.py

    Run commands using:

    (lldb) pybt
        ...
    (lldb) pybtall
        ...
    (lldb) pylocals
        ...

    or inside the python shell:

    (lldb) script
    Python Interactive Interpreter. To exit, type 'quit()', 'exit()' or Ctrl-D.
    >>> lldb_utils.pybt()
        ...
    >>> lldb_utils.pybtall()
        ...
    >>> lldb_utils.pylocals()
        ...

pybt - generate a python function call back trace of the currently selected thread
pybtall - generate a python function call back trace of all threads
pylocals - generate a list of the name and values of all locals in the current
    python frame (only works when the currently selected frame is a Python call
    frame as found by the pybt command).
"""
from __future__ import print_function

import lldb  # @UnresolvedImport


def _toStr(obj, pystring_t):
    return obj.Cast(pystring_t).GetValueForExpressionPath("->ob_sval").summary


def pybt(debugger=None, command=None, result=None, dict=None, thread=None):
    """
    An lldb command that prints a Python call back trace for the specified
    thread or the currently selected thread.

    @param debugger: debugger to use
    @type debugger: L{lldb.SBDebugger}
    @param command: ignored
    @type command: ignored
    @param result: ignored
    @type result: ignored
    @param dict: ignored
    @type dict: ignored
    @param thread: the specific thread to target
    @type thread: L{lldb.SBThread}
    """

    if debugger is None:
        debugger = lldb.debugger
    target = debugger.GetSelectedTarget()
    if not isinstance(thread, lldb.SBThread):
        thread = target.GetProcess().GetSelectedThread()

    pystring_t = target.FindFirstType("PyStringObject").GetPointerType()

    num_frames = thread.GetNumFrames()
    for i in range(num_frames - 1):
        fr = thread.GetFrameAtIndex(i)
        if fr.GetFunctionName() == "PyEval_EvalFrameEx":
            fr_next = thread.GetFrameAtIndex(i + 1)
            if fr_next.GetFunctionName() == "PyEval_EvalCodeEx":
                f = fr.GetValueForVariablePath("f")
                filename = _toStr(f.GetValueForExpressionPath("->f_code->co_filename"), pystring_t)
                name = _toStr(f.GetValueForExpressionPath("->f_code->co_name"), pystring_t)
                lineno = f.GetValueForExpressionPath("->f_lineno").GetValue()
                print("#{}: {} - {}:{}".format(
                    fr.GetFrameID(),
                    filename[1:-1] if filename else ".",
                    name[1:-1] if name else ".",
                    lineno if lineno else ".",
                ))


def pybtall(debugger=None, command=None, result=None, dict=None):
    """
    An lldb command that prints a Python call back trace for all threads.

    @param debugger: debugger to use
    @type debugger: L{lldb.SBDebugger}
    @param command: ignored
    @type command: ignored
    @param result: ignored
    @type result: ignored
    @param dict: ignored
    @type dict: ignored
    """
    if debugger is None:
        debugger = lldb.debugger
    process = debugger.GetSelectedTarget().GetProcess()
    numthreads = process.GetNumThreads()
    for i in range(numthreads):
        thread = process.GetThreadAtIndex(i)
        print("----- Thread: {} -----".format(i + 1))
        pybt(debugger=debugger, thread=thread)


def pylocals(debugger=None, command=None, result=None, dict=None):
    """
    An lldb command that prints a list of Python local variables for the
    currently selected frame.

    @param debugger: debugger to use
    @type debugger: L{lldb.SBDebugger}
    @param command: ignored
    @type command: ignored
    @param result: ignored
    @type result: ignored
    @param dict: ignored
    @type dict: ignored
    """
    if debugger is None:
        debugger = lldb.debugger
    target = debugger.GetSelectedTarget()
    frame = target.GetProcess().GetSelectedThread().GetSelectedFrame()

    pystring_t = target.FindFirstType("PyStringObject").GetPointerType()
    pytuple_t = target.FindFirstType("PyTupleObject").GetPointerType()

    f = frame.GetValueForVariablePath("f")
    try:
        numlocals = int(f.GetValueForExpressionPath("->f_code->co_nlocals").GetValue())
    except TypeError:
        print("Current frame is not a Python function")
        return
    print("Locals in frame #{}".format(frame.GetFrameID()))
    tstate = frame.EvaluateExpression("PyGILState_Ensure()")
    names = f.GetValueForExpressionPath("->f_code->co_varnames").Cast(pytuple_t)
    for i in range(numlocals):
        localname = _toStr(names.GetValueForExpressionPath("->ob_item[{}]".format(i)), pystring_t)
        local = frame.EvaluateExpression("PyString_AsString(PyObject_Repr(f->f_localsplus[{}]))".format(i)).summary
        localtype = frame.EvaluateExpression("PyString_AsString(PyObject_Repr(PyObject_Type(f->f_localsplus[{}])))".format(i)).summary
        print("{}: {} = {}".format(
            localtype[1:-1] if localtype else ".",
            localname[1:-1] if localname else ".",
            local[1:-1] if local else ".",
        ))
    frame.EvaluateExpression("PyGILState_Release({})".format(tstate))


CMDS = ("pybt", "pybtall", "pylocals",)


def __lldb_init_module(debugger, dict):
    """
    Register each command with lldb so they are available directly within lldb as
    well as within its Python script shell.

    @param debugger: debugger to use
    @type debugger: L{lldb.SBDebugger}
    @param dict: ignored
    @type dict: ignored
    """
    for cmd in CMDS:
        debugger.HandleCommand(
            "command script add -f lldb_utils.{cmd} {cmd}".format(cmd=cmd)
        )
