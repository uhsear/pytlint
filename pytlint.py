#!/usr/bin/env python
"""Lint an ArcGIS Python toolbox (.pyt) by parsing it, never by importing it.

A .pyt is a Python file, so every failure in it is a runtime failure, and Pro
reports almost none of them. The two that cost the most time:

    def getParameterInfo(self):
        return [p0, p1, p2, p3]          # four parameters
    def updateMessages(self, parameters):
        if parameters[4].value:          # IndexError, swallowed by Pro
            ...

The dialog greys out or refuses to open, with no traceback and no message. The
second flavour is worse, because it succeeds: execute calls arcpy.AddError(...)
and then returns instead of raising, so the tool prints red text, reports
"Completed successfully", and the scheduled task wrapping it exits 0 forever.

pylint and flake8 are better Python linters than this will ever be, and you
should run them too. They do not know what a .pyt is. They cannot tell you that
parameters[4] is past the end, because the length is decided by a different
method in the same class, and they have no reason to care that a bare return
after AddError is the difference between a failed nightly job and a silent one.
The ArcGIS-specific alternative is to open the toolbox in Pro and click every
tool, which needs Pro, a licence, and a person.

This reads the file with the ast module. It never imports the toolbox, so no
module-level code runs, no arcpy is needed, and it works in CI on a machine
with no Esri software on it.

    python pytlint.py --self-test
    python pytlint.py MyTools.pyt
    python pytlint.py toolboxes/*.pyt --json
    python pytlint.py MyTools.pyt --select PYT001,PYT006

Exit codes: 0 clean, 1 an error-severity finding, 2 a file could not be read,
64 usage error.
"""

from __future__ import print_function

import argparse
import ast
import json
import sys

from pathlib import Path

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Methods arcpy calls on every tool class. A tool without these three never runs.
REQUIRED_TOOL_METHODS = ("__init__", "getParameterInfo", "execute")

# Signatures arcpy calls positionally. The arity is what breaks. The names are
# checked too, because a wrong name here is always a copy-paste from a different
# hook, and the next person to read the file needs them to match the documentation.
EXPECTED_SIGNATURES = {
    "execute": ("self", "parameters", "messages"),
    "updateParameters": ("self", "parameters"),
    "updateMessages": ("self", "parameters"),
}

# The only values arcpy.Parameter documents for these two keywords.
VALID_PARAMETER_TYPES = ("Required", "Optional", "Derived")
VALID_DIRECTIONS = ("Input", "Output")

# Methods that receive the parameter list and can therefore index past its end.
PARAMETER_LIST_METHODS = ("updateParameters", "updateMessages", "execute")

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# Code, severity, one line of description. Severity decides the exit code, so a
# rule is an error only when the finding is certain breakage, not a smell.
RULES = (
    ("PYT000", "error", "the file does not parse as Python"),
    ("PYT001", "error", "a parameter index is past the end of getParameterInfo"),
    ("PYT002", "error", "two parameters in one tool share a name"),
    ("PYT003", "warning", "a Required parameter is declared after an Optional one"),
    ("PYT004", "warning", "updateMessages assigns .value, which Pro discards"),
    ("PYT005", "warning", "an Output parameter is never written in execute"),
    ("PYT006", "error", "AddError is followed by a bare return instead of a raise"),
    ("PYT007", "warning", "a tool class has no isLicensed"),
    ("PYT008", "error", "self.tools names a class that is not defined in this file"),
    ("PYT009", "error", "a tool class is missing a method arcpy calls"),
    ("PYT010", "warning", "a class has no self.label or no self.description"),
    ("PYT011", "error", "execute has the wrong signature"),
    ("PYT012", "error", "the file defines no Toolbox class"),
    ("PYT013", "error", "the Toolbox class never assigns self.tools"),
    ("PYT014", "error", "getParameterInfo returns nothing"),
    ("PYT015", "error", "a parameterType or direction value is not a documented one"),
    ("PYT016", "error", "updateParameters or updateMessages has the wrong signature"),
    ("PYT017", "warning", "self.canRunInBackground is set, and Pro ignores it"),
    ("PYT018", "warning", "the Toolbox class has no self.alias"),
)

RULE_SEVERITY = dict((code, severity) for code, severity, _ in RULES)
ALL_CODES = tuple(code for code, _, _ in RULES)


class Finding(object):
    """One diagnostic, with everything needed to print it as a compiler line."""

    def __init__(self, code, line, message):
        self.code = code
        self.severity = RULE_SEVERITY[code]
        self.line = line
        self.message = message

    @property
    def sort_key(self):
        return (self.line, self.code)

    def as_dict(self, path=None):
        out = {"line": self.line, "code": self.code,
               "severity": self.severity, "message": self.message}
        if path is not None:
            out["path"] = str(path)
        return out

    def __repr__(self):
        return "Finding(%s, line=%d, %r)" % (self.code, self.line, self.message)


# --------------------------------------------------------------- ast helpers

def _dotted(node):
    """Dotted name for a Name or Attribute chain, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _const_index(subscript):
    """Non-negative literal int index of a Subscript, else None.

    Negative indices are skipped on purpose. parameters[-1] is in range for
    every non-empty list, so reporting it would be noise.
    """
    sliced = subscript.slice
    # Python 3.8 wraps a simple subscript in ast.Index. 3.9 and later do not.
    if sliced.__class__.__name__ == "Index":
        sliced = sliced.value
    if isinstance(sliced, ast.Constant) and isinstance(sliced.value, int) \
            and not isinstance(sliced.value, bool) and sliced.value >= 0:
        return sliced.value
    return None


def _string(node):
    """The value of a string literal node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _keyword(call, name):
    """String value of a keyword argument on a call, else None."""
    for kw in call.keywords:
        if kw.arg == name:
            return _string(kw.value)
    return None


def _methods(cls):
    """Method name to FunctionDef. A redefinition wins, as Python decides it."""
    found = {}
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[stmt.name] = stmt
    return found


def _self_assignments(cls):
    """Every 'self.x = ...' anywhere in the class, as name to its first Assign."""
    found = {}
    for node in ast.walk(cls):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and \
                    isinstance(target.value, ast.Name) and \
                    target.value.id == "self" and target.attr not in found:
                found[target.attr] = node
    return found


def _arg_names(fn):
    return tuple(arg.arg for arg in fn.args.args)


def _param_arg(fn, position=1):
    """The name this method gave its parameter list, else None.

    Read from the signature instead of assumed to be "parameters", because a
    tool that called it "params" still has to be checked for an index overrun.
    """
    names = _arg_names(fn)
    if len(names) > position:
        return names[position]
    return None


def _subscripts_of(fn, name):
    """(index, node) for every name[<literal int>] inside fn."""
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and node.value.id == name:
            index = _const_index(node)
            if index is not None:
                out.append((index, node))
    return out


def _statement_blocks(fn):
    """Every statement list inside fn, so the order within a block stays readable."""
    for node in ast.walk(fn):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block


def _is_add_error(stmt):
    """An arcpy.AddError(...) or messages.addErrorMessage(...) statement."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    name = _dotted(stmt.value.func)
    if not name:
        return False
    return name.split(".")[-1] in ("AddError", "AddErrorMessage", "addErrorMessage")


def _is_bare_return(stmt):
    """return, or return None. Either one hands Pro a success."""
    if not isinstance(stmt, ast.Return):
        return False
    return stmt.value is None or (isinstance(stmt.value, ast.Constant)
                                  and stmt.value.value is None)


# ----------------------------------------------------------------- parameters

class ParamInfo(object):
    """One arcpy.Parameter(...) construction found in getParameterInfo."""

    def __init__(self, line, name, ptype, direction):
        self.line = line
        self.name = name
        self.ptype = ptype
        self.direction = direction

    def __repr__(self):
        return "ParamInfo(line=%d, name=%r, %s/%s)" % (
            self.line, self.name, self.ptype, self.direction)


def _is_parameter_call(node):
    """arcpy.Parameter(...), or a bare Parameter(...) from a direct import."""
    if not isinstance(node, ast.Call):
        return False
    name = _dotted(node.func)
    return bool(name) and name.split(".")[-1] == "Parameter"


def _mutated_names(fn):
    """Names grown by append, extend, insert or +=, anywhere inside fn.

    A list filled by params.append(...) in a loop has a length no parser can
    know, so the name is not a trustworthy parameter count even when it was
    first assigned a literal list.
    """
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("append", "extend", "insert") \
                and isinstance(node.func.value, ast.Name):
            out.add(node.func.value.id)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def _literal_lengths(fn):
    """Name to every literal list or tuple length assigned to it inside fn."""
    out = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or \
                not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out.setdefault(target.id, []).append(len(node.value.elts))
    return out


def _returned_count(fn):
    """How many parameters getParameterInfo really hands arcpy, or None.

    None means the length is not decidable from the source: a list built in a
    loop, a helper call, a name that never held a literal list. Every index
    rule is skipped there on purpose.

    Counting the arcpy.Parameter(...) calls instead is the tempting fallback and
    it is wrong. One Parameter built inside a for loop is one call and five
    parameters, so that fallback reported parameters[4] as past the end of a
    tool that works. An error-severity false report on correct code costs more
    than the miss, because it is the report that makes people stop running the
    linter at all.
    """
    mutated = _mutated_names(fn)
    lengths = _literal_lengths(fn)
    counts = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        if isinstance(value, (ast.List, ast.Tuple)):
            counts.append(len(value.elts))
        elif isinstance(value, ast.Name) and value.id in lengths \
                and value.id not in mutated:
            # The Esri template shape: params = [param0, param1]; return params.
            counts.append(max(lengths[value.id]))
        else:
            return None
    if not counts:
        return None
    # Two returns of different lengths. Take the longest, so an index that is
    # valid on one branch is never reported as past the end.
    return max(counts)


def _collect_parameters(fn):
    """The parameters in source order, and the count the tool really gets.

    The count comes from what getParameterInfo returns, because that is what
    arcpy receives. A Parameter that is built and never returned is a real
    pattern in a half-finished toolbox, and counting it would inflate the length
    and hide the index overrun this tool exists to find.
    """
    params = []
    for node in ast.walk(fn):
        if _is_parameter_call(node):
            params.append(ParamInfo(node.lineno,
                                    _keyword(node, "name"),
                                    _keyword(node, "parameterType"),
                                    _keyword(node, "direction")))
    params.sort(key=lambda p: p.line)
    return params, _returned_count(fn)


def _written_indexes(fn, pname):
    """Indexes execute writes, either as an attribute or through SetParameter."""
    written = set()
    if pname is None:
        return written
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and \
                        isinstance(target.value, ast.Subscript) and \
                        isinstance(target.value.value, ast.Name) and \
                        target.value.value.id == pname:
                    index = _const_index(target.value)
                    if index is not None:
                        written.add(index)
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name and name.split(".")[-1] in ("SetParameter", "SetParameterAsText") \
                    and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, int):
                written.add(node.args[0].value)
    return written


# ------------------------------------------------------------------ pure core

def _check_signatures(methods, out):
    """Arity and argument names against what arcpy calls positionally."""
    for name, expected in EXPECTED_SIGNATURES.items():
        fn = methods.get(name)
        if fn is None:
            continue
        actual = _arg_names(fn)
        if actual == expected:
            continue
        code = "PYT011" if name == "execute" else "PYT016"
        out.append(Finding(code, fn.lineno,
                           "%s(%s) does not match %s(%s), which is the "
                           "signature arcpy calls"
                           % (name, ", ".join(actual), name, ", ".join(expected))))


def _check_parameters(cls_name, params, out):
    """Everything decided by the Parameter constructions on their own."""
    seen = {}
    for param in params:
        if param.name is None:
            continue
        if param.name in seen:
            out.append(Finding("PYT002", param.line,
                               "%s declares the parameter name %r twice, first "
                               "at line %d. Every lookup by that name reaches "
                               "one of them and the other is unreachable."
                               % (cls_name, param.name, seen[param.name])))
        else:
            seen[param.name] = param.line

    optional_at = None
    for param in params:
        if param.ptype == "Optional" and optional_at is None:
            optional_at = param.line
        elif param.ptype == "Required" and optional_at is not None:
            out.append(Finding("PYT003", param.line,
                               "Required parameter %r follows the Optional one "
                               "at line %d. The dialog lists optional parameters "
                               "last, so the order the user sees is not this one."
                               % (param.name, optional_at)))

    for param in params:
        if param.ptype is not None and param.ptype not in VALID_PARAMETER_TYPES:
            out.append(Finding("PYT015", param.line,
                               "parameterType=%r is not one of %s"
                               % (param.ptype, ", ".join(VALID_PARAMETER_TYPES))))
        if param.direction is not None and param.direction not in VALID_DIRECTIONS:
            out.append(Finding("PYT015", param.line,
                               "direction=%r is not one of %s"
                               % (param.direction, ", ".join(VALID_DIRECTIONS))))


def _check_tool_class(cls, out, is_toolbox=False):
    """Every rule that applies to one class. Appends to out, returns nothing."""
    methods = _methods(cls)
    assigns = _self_assignments(cls)

    for attr in ("label", "description"):
        if attr not in assigns:
            out.append(Finding("PYT010", cls.lineno,
                               "%s never assigns self.%s, so the dialog shows "
                               "the class name and the help pane is empty"
                               % (cls.name, attr)))

    if "canRunInBackground" in assigns:
        out.append(Finding("PYT017", assigns["canRunInBackground"].lineno,
                           "self.canRunInBackground is an ArcMap setting. Pro "
                           "ignores it, so this line promises something it does "
                           "not do."))

    if is_toolbox:
        if "alias" not in assigns:
            out.append(Finding("PYT018", cls.lineno,
                               "Toolbox never assigns self.alias, so the "
                               "toolbox cannot be called from arcpy by name"))
        return

    for name in REQUIRED_TOOL_METHODS:
        if name not in methods:
            out.append(Finding("PYT009", cls.lineno,
                               "%s has no %s. arcpy calls it on every tool."
                               % (cls.name, name)))

    if "isLicensed" not in methods:
        out.append(Finding("PYT007", cls.lineno,
                           "%s has no isLicensed. The tool then stays enabled "
                           "whatever extension it needs, and fails at run time "
                           "instead of greying out." % cls.name))

    _check_signatures(methods, out)

    # None means the count is not decidable from the source. Every index rule
    # below stays quiet then, rather than guessing at a length.
    params, count = [], None
    get_info = methods.get("getParameterInfo")
    if get_info is not None:
        params, count = _collect_parameters(get_info)
        returns_value = any(isinstance(node, ast.Return) and node.value is not None
                            for node in ast.walk(get_info))
        if not returns_value:
            out.append(Finding("PYT014", get_info.lineno,
                               "%s.getParameterInfo returns nothing, so arcpy "
                               "receives None where it expects a list of "
                               "parameters" % cls.name))
        _check_parameters(cls.name, params, out)

    # ---- the index overrun, the headline rule
    if count is not None:
        for method_name in PARAMETER_LIST_METHODS:
            fn = methods.get(method_name)
            if fn is None:
                continue
            pname = _param_arg(fn)
            if pname is None:
                continue
            for index, node in _subscripts_of(fn, pname):
                if index >= count:
                    out.append(Finding(
                        "PYT001", node.lineno,
                        "%s.%s reads %s[%d] but getParameterInfo returns %d "
                        "parameter(s), so the highest valid index is %d. Pro "
                        "swallows the IndexError and the dialog will not open."
                        % (cls.name, method_name, pname, index, count,
                           count - 1)))

    # ---- a value written where Pro has already read it
    update_messages = methods.get("updateMessages")
    if update_messages is not None:
        pname = _param_arg(update_messages)
        for node in ast.walk(update_messages):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and \
                        target.attr in ("value", "values") and \
                        isinstance(target.value, ast.Subscript) and \
                        isinstance(target.value.value, ast.Name) and \
                        target.value.value.id == pname:
                    out.append(Finding("PYT004", node.lineno,
                                       "updateMessages assigns .%s. Pro has "
                                       "already read the values by this point "
                                       "and discards the change. Set a value in "
                                       "updateParameters instead." % target.attr))

    execute = methods.get("execute")
    if execute is None:
        return

    # ---- an Output parameter execute forgets to fill in
    # Skipped with an undecidable count, because the position a parameter holds
    # in the returned list is the index execute writes, and that is unknown.
    written = _written_indexes(execute, _param_arg(execute))
    for index, param in enumerate(params):
        if count is None or index >= count:
            break
        if param.direction == "Output" and index not in written:
            out.append(Finding("PYT005", param.line,
                               "parameter %r is direction=Output but execute "
                               "never assigns %s[%d]. Anything downstream in a "
                               "model receives an empty result."
                               % (param.name,
                                  _param_arg(execute) or "parameters", index)))

    # ---- AddError then return, the failure that reports success
    for block in _statement_blocks(execute):
        error_line = None
        for stmt in block:
            if _is_add_error(stmt):
                error_line = stmt.lineno
            elif isinstance(stmt, ast.Raise):
                error_line = None
            elif error_line is not None and _is_bare_return(stmt):
                out.append(Finding(
                    "PYT006", stmt.lineno,
                    "execute reports an error at line %d and then returns. The "
                    "tool prints red text and still reports success, so the "
                    "scheduled task wrapping it exits 0. Raise "
                    "arcpy.ExecuteError instead." % error_line))
                error_line = None


def check_source(source):
    """Every finding for one toolbox source.

    Pure: no file, no arcpy, no network, and the toolbox is never imported, so
    nothing in it executes.
    """
    # Windows editors save a .pyt with a UTF-8 byte order mark, and Python
    # strips that mark when it imports the file. ast.parse does not strip it
    # from a string, so leaving it here reports PYT000 on a toolbox that loads.
    if source.startswith("\ufeff"):
        source = source[1:]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding("PYT000", exc.lineno or 1, exc.msg or "invalid syntax")]

    out = []
    classes = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            classes[stmt.name] = stmt

    toolbox = classes.get("Toolbox")
    tool_names = []
    if toolbox is None:
        out.append(Finding("PYT012", 1,
                           "no class named Toolbox. Pro looks that name up as a "
                           "string and shows an empty toolbox when it is absent."))
        # Lint every other class anyway, so one missing name does not hide the
        # rest of the file behind a single finding.
        tool_names = [name for name in classes if name != "Toolbox"]
    else:
        _check_tool_class(toolbox, out, is_toolbox=True)
        tools_assign = _self_assignments(toolbox).get("tools")
        if tools_assign is None:
            out.append(Finding("PYT013", toolbox.lineno,
                               "Toolbox never assigns self.tools, so the "
                               "toolbox holds no tools"))
        elif isinstance(tools_assign.value, (ast.List, ast.Tuple)):
            for element in tools_assign.value.elts:
                if isinstance(element, ast.Name):
                    # A name listed twice is linted once. Pro loads the class
                    # once, and every finding reported twice is noise.
                    if element.id not in tool_names:
                        tool_names.append(element.id)
                    if element.id not in classes:
                        out.append(Finding(
                            "PYT008", tools_assign.lineno,
                            "self.tools names %s, which is not defined in this "
                            "file. Pro fails to load the whole toolbox, not "
                            "just that one tool." % element.id))

    for name in tool_names:
        cls = classes.get(name)
        if cls is not None:
            _check_tool_class(cls, out)

    out.sort(key=lambda finding: finding.sort_key)
    return out


def filter_findings(findings, select=None, ignore=None):
    """Keep the selected codes, then drop the ignored ones. Ignore wins."""
    kept = list(findings)
    if select:
        kept = [f for f in kept if f.code in select]
    if ignore:
        kept = [f for f in kept if f.code not in ignore]
    return kept


def exit_code(findings, strict=False):
    """1 when something here will actually break, else 0."""
    for finding in findings:
        if finding.severity == "error" or strict:
            return 1
    return 0


def format_finding(finding, path):
    """The compiler-shaped line an editor can jump to."""
    return "%s:%d: %s %s: %s" % (path, finding.line, finding.code,
                                 finding.severity, finding.message)


def parse_codes(text, flag):
    """Split a comma separated code list. An unknown code is a usage error."""
    if not text:
        return set()
    codes = set()
    for piece in text.split(","):
        code = piece.strip().upper()
        if not code:
            continue
        if code not in RULE_SEVERITY:
            raise ValueError("%s: %s is not a rule code. Use --list-rules."
                             % (flag, code))
        codes.add(code)
    return codes


# ------------------------------------------------------------------ self-test

# Every rule gets one embedded .pyt source and its negative twin. The line the
# rule must fire on carries a "#@" marker, so each assertion pins the reported
# line number without anybody counting lines by hand.

HEAD = '''import arcpy


class Toolbox(object):
    def __init__(self):
        self.label = "Demo"
        self.alias = "demo"
        self.description = "Demo toolbox"
        self.tools = [%s]

'''

GOOD_TOOL = '''class Demo(object):
    def __init__(self):
        self.label = "Demo tool"
        self.description = "Does one thing"

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        p0 = arcpy.Parameter(displayName="In", name="in_fc",
                             datatype="GPFeatureLayer",
                             parameterType="Required", direction="Input")
        p1 = arcpy.Parameter(displayName="Out", name="out_fc",
                             datatype="DEFeatureClass",
                             parameterType="Required", direction="Output")
        return [p0, p1]

    def updateParameters(self, parameters):
        if parameters[0].altered:
            parameters[1].value = "out"

    def updateMessages(self, parameters):
        if parameters[1].value:
            parameters[1].clearMessage()

    def execute(self, parameters, messages):
        parameters[1].value = parameters[0].valueAsText
'''


def _pyt(body, tools="Demo"):
    """Wrap a tool class in a valid Toolbox, so only the rule under test fires."""
    return HEAD % tools + body


def _marked(source):
    for number, line in enumerate(source.splitlines(), 1):
        if "#@" in line:
            return number
    raise AssertionError("this test source carries no #@ marker")


def _lines_for(source, code):
    return [f.line for f in check_source(source) if f.code == code]


def _message_for(source, code):
    return [f.message for f in check_source(source) if f.code == code][0]


def _fires(source, code):
    """The rule fires exactly once, on the marked line."""
    return _lines_for(source, code) == [_marked(source)]


def _silent(source, code):
    return _lines_for(source, code) == []


CLEAN = _pyt(GOOD_TOOL)


def self_test():
    """Assertions over the decision core. No arcpy, no toolbox file, no network."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("pytlint self-test: no arcpy, no toolbox file, no network")
    print("-" * 68)

    # ---- a toolbox with nothing wrong with it
    check(check_source(CLEAN) == [],
          "a correct toolbox produces no findings at all")

    # ---- the index overrun, the headline rule
    bad_index = _pyt('''class Demo(object):
    def getParameterInfo(self):
        p0 = arcpy.Parameter(name="a", parameterType="Required", direction="Input")
        p1 = arcpy.Parameter(name="b", parameterType="Required", direction="Input")
        return [p0, p1]

    def updateMessages(self, parameters):
        if parameters[4].value:  #@
            pass
''')
    check(_fires(bad_index, "PYT001"),
          "an index past the returned parameter count fires on the reading line")
    check(_silent(bad_index.replace("parameters[4]", "parameters[1]"), "PYT001"),
          "the last valid index is silent")
    check("[4]" in _message_for(bad_index, "PYT001")
          and "returns 2" in _message_for(bad_index, "PYT001"),
          "the message names both the index used and the count returned")
    check("highest valid index is 1" in _message_for(bad_index, "PYT001"),
          "the message names the highest index that would have worked")
    check(_fires(bad_index.replace("parameters[4]", "parameters[2]"), "PYT001"),
          "an index exactly one past the end fires  <-- pinned defect")
    check(_silent(bad_index.replace("parameters[4]", "parameters[-1]"), "PYT001"),
          "a negative index is in range and stays silent")
    check(_silent(bad_index.replace("parameters[4]", "parameters[i]"), "PYT001"),
          "an index computed at run time is not guessed at")

    renamed_list = _pyt('''class Demo(object):
    def getParameterInfo(self):
        p0 = arcpy.Parameter(name="a", parameterType="Required", direction="Input")
        return [p0]

    def updateParameters(self, params):
        params[3].value = 1  #@
''')
    check(_fires(renamed_list, "PYT001"),
          "the parameter list is found under whatever name the method gave it")

    unreturned = _pyt('''class Demo(object):
    def getParameterInfo(self):
        p0 = arcpy.Parameter(name="a", parameterType="Required", direction="Input")
        p1 = arcpy.Parameter(name="b", parameterType="Required", direction="Input")
        return [p0]

    def updateMessages(self, parameters):
        if parameters[1].value:  #@
            pass
''')
    check(_fires(unreturned, "PYT001"),
          "a Parameter built but never returned does not raise the count  <-- pinned defect")

    # ---- the parameter count, and refusing to guess one
    # Every false PYT001 below was produced by the earlier version, which
    # counted arcpy.Parameter(...) calls when the return was not a literal list.
    esri = _pyt('''class Demo(object):
    def getParameterInfo(self):
        param0 = arcpy.Parameter(name="a", parameterType="Required", direction="Input")
        param1 = arcpy.Parameter(name="b", parameterType="Required", direction="Input")
        params = [param0, param1]
        return params

    def updateMessages(self, parameters):
        if parameters[2].value:  #@
            pass
''')
    check(_fires(esri, "PYT001"),
          "the params = [p0, p1] then return params shape is still counted")
    check(_silent(esri.replace("parameters[2]", "parameters[1]"), "PYT001"),
          "the last valid index of that shape is silent")

    loop = _pyt('''class Demo(object):
    def getParameterInfo(self):
        params = []
        for name in ("a", "b", "c", "d", "e"):
            params.append(arcpy.Parameter(name=name, parameterType="Required",
                                          direction="Input"))
        return params

    def updateMessages(self, parameters):
        if parameters[4].value:
            pass
''')
    check(_silent(loop, "PYT001"),
          "a list built by append in a loop is not counted at all  <-- pinned defect")
    check(_silent(loop.replace("parameters[4]", "parameters[40]"), "PYT001"),
          "no index is reported against a loop-built list, however large")

    helper = _pyt('''class Demo(object):
    def getParameterInfo(self):
        return self._build_parameters()

    def updateMessages(self, parameters):
        if parameters[2].value:
            pass
''')
    check(_silent(helper, "PYT001"),
          "parameters built by a helper call are not counted  <-- pinned defect")

    no_info = _pyt('''class Demo(object):  #@
    def execute(self, parameters, messages):
        arcpy.AddMessage(parameters[0].valueAsText)
''')
    check(_silent(no_info, "PYT001"),
          "a class with no getParameterInfo reports no index overrun  <-- pinned defect")
    check("PYT009" in [f.code for f in check_source(no_info)],
          "that class is still reported for the missing method")

    two_returns = _pyt('''class Demo(object):
    def getParameterInfo(self):
        p0 = arcpy.Parameter(name="a", parameterType="Required", direction="Input")
        p1 = arcpy.Parameter(name="b", parameterType="Required", direction="Input")
        if arcpy.CheckExtension("Spatial") == "Available":
            return [p0, p1]
        return [p0]

    def updateMessages(self, parameters):
        if parameters[1].value:
            pass
''')
    check(_silent(two_returns, "PYT001"),
          "an index valid on the longer of two returns is not reported")
    check(_fires(two_returns.replace("parameters[1].value:",
                                     "parameters[2].value:  #@"), "PYT001"),
          "an index past both returns still fires")

    # ---- two parameters with one name
    dupe = _pyt('''class Demo(object):
    def getParameterInfo(self):
        p0 = arcpy.Parameter(name="in_fc", parameterType="Required", direction="Input")
        p1 = arcpy.Parameter(name="in_fc", parameterType="Optional", direction="Input")  #@
        return [p0, p1]
''')
    check(_fires(dupe, "PYT002"),
          "a repeated parameter name fires on the second declaration")
    check(_silent(dupe.replace('name="in_fc", parameterType="Optional"',
                               'name="out_fc", parameterType="Optional"'), "PYT002"),
          "two different parameter names are silent")

    # ---- Required after Optional
    order = _pyt('''class Demo(object):
    def getParameterInfo(self):
        p0 = arcpy.Parameter(name="a", parameterType="Optional", direction="Input")
        p1 = arcpy.Parameter(name="b", parameterType="Required", direction="Input")  #@
        return [p0, p1]
''')
    check(_fires(order, "PYT003"),
          "a Required parameter after an Optional one fires on the Required one")
    check(_silent(order.replace('name="b", parameterType="Required"',
                                'name="b", parameterType="Optional"'), "PYT003"),
          "Optional after Optional is silent")
    check(_silent(_pyt('''class Demo(object):
    def getParameterInfo(self):
        p0 = arcpy.Parameter(name="a", parameterType="Required", direction="Input")
        p1 = arcpy.Parameter(name="b", parameterType="Optional", direction="Input")
        return [p0, p1]
'''), "PYT003"),
          "Required before Optional, the order the dialog uses, is silent")

    # ---- a value set where Pro has already read it
    late_value = _pyt('''class Demo(object):
    def getParameterInfo(self):
        p0 = arcpy.Parameter(name="a", parameterType="Required", direction="Input")
        return [p0]

    def updateMessages(self, parameters):
        parameters[0].value = 3  #@
''')
    check(_fires(late_value, "PYT004"),
          "an assignment to .value inside updateMessages fires on that line")
    check(_silent(late_value.replace("parameters[0].value = 3  #@",
                                     "parameters[0].setErrorMessage('no')  #@"),
                  "PYT004"),
          "setting a message inside updateMessages is what the hook is for")
    check(_silent(late_value.replace("def updateMessages(self, parameters):",
                                     "def updateParameters(self, parameters):"),
                  "PYT004"),
          "the same assignment inside updateParameters is silent")

    # ---- an Output parameter execute forgets
    unwritten = _pyt('''class Demo(object):
    def getParameterInfo(self):
        p0 = arcpy.Parameter(name="a", parameterType="Required", direction="Input")
        p1 = arcpy.Parameter(name="out", parameterType="Derived", direction="Output")  #@
        return [p0, p1]

    def execute(self, parameters, messages):
        arcpy.AddMessage("done")
''')
    check(_fires(unwritten, "PYT005"),
          "an Output parameter execute never writes fires on its declaration")
    check(_silent(unwritten.replace('arcpy.AddMessage("done")',
                                    'parameters[1].value = "x"'), "PYT005"),
          "assigning the Output parameter in execute is silent")
    check(_silent(unwritten.replace('arcpy.AddMessage("done")',
                                    'arcpy.SetParameterAsText(1, "x")'), "PYT005"),
          "SetParameterAsText counts as writing the Output parameter")
    check(_silent(unwritten.replace('direction="Output")  #@',
                                    'direction="Input")  #@'), "PYT005"),
          "an Input parameter execute never writes is silent")

    # ---- AddError then return, the run that reports success
    soft_fail = _pyt('''class Demo(object):
    def getParameterInfo(self):
        p0 = arcpy.Parameter(name="a", parameterType="Required", direction="Input")
        return [p0]

    def execute(self, parameters, messages):
        if not parameters[0].value:
            arcpy.AddError("no input")
            return  #@
        arcpy.AddMessage("ok")
''')
    check(_fires(soft_fail, "PYT006"),
          "AddError followed by a bare return fires on the return")
    check("exits 0" in _message_for(soft_fail, "PYT006"),
          "the message says the caller still sees a success")
    check(_silent(soft_fail.replace("            return  #@",
                                    "            raise arcpy.ExecuteError  #@"),
                  "PYT006"),
          "raising after AddError is the correct form and stays silent")
    check(_silent(soft_fail.replace('arcpy.AddError("no input")',
                                    'arcpy.AddMessage("no input")'), "PYT006"),
          "a plain message followed by a return is silent")
    check(_fires(soft_fail.replace('arcpy.AddError("no input")',
                                   'messages.addErrorMessage("no input")'), "PYT006"),
          "the messages.addErrorMessage form fires the same way")
    check(_fires(soft_fail.replace("            return  #@",
                                   "            return None  #@"), "PYT006"),
          "an explicit return None is the same defect")

    # ---- isLicensed
    no_licence = _pyt('''class Demo(object):  #@
    def __init__(self):
        self.label = "x"
        self.description = "y"

    def getParameterInfo(self):
        return []

    def execute(self, parameters, messages):
        pass
''')
    check(_fires(no_licence, "PYT007"),
          "a tool class with no isLicensed fires on the class line")
    check(_silent(no_licence.replace("    def getParameterInfo(self):",
                                     "    def isLicensed(self):\n"
                                     "        return True\n\n"
                                     "    def getParameterInfo(self):"), "PYT007"),
          "a tool class that defines isLicensed is silent")

    # ---- self.tools naming a class that is not there
    ghost = _pyt(GOOD_TOOL, tools="Demo, Missing")
    check(_lines_for(ghost, "PYT008") == [9],
          "a tool name with no class fires on the self.tools line")
    check(_silent(CLEAN, "PYT008"),
          "a tool name that matches a class in the file is silent")
    twice = _pyt(GOOD_TOOL.replace("    def isLicensed(self):\n"
                                   "        return True\n\n", ""),
                 tools="Demo, Demo")
    check(_lines_for(twice, "PYT007") == _lines_for(_pyt(
        GOOD_TOOL.replace("    def isLicensed(self):\n"
                          "        return True\n\n", "")), "PYT007"),
          "a class named twice in self.tools is linted once  <-- pinned defect")

    # ---- the methods arcpy calls
    stub = _pyt('''class Demo(object):  #@
    def isLicensed(self):
        return True
''')
    check(_lines_for(stub, "PYT009") == [_marked(stub)] * 3,
          "a class with no __init__, getParameterInfo or execute fires three times")
    check(_silent(CLEAN, "PYT009"),
          "a complete tool class is silent")

    # ---- label, description, and a setting Pro dropped
    unlabelled = _pyt('''class Demo(object):  #@
    def __init__(self):
        self.canRunInBackground = False

    def getParameterInfo(self):
        return []

    def execute(self, parameters, messages):
        pass
''')
    check(_lines_for(unlabelled, "PYT010") == [_marked(unlabelled)] * 2,
          "a class with neither label nor description fires twice on the class line")
    check(_silent(CLEAN, "PYT010"),
          "a labelled tool inside a labelled Toolbox is silent")
    check(_lines_for(unlabelled, "PYT017") == [_marked(unlabelled) + 2],
          "canRunInBackground fires on its own assignment line")
    check(_silent(CLEAN, "PYT017"),
          "a toolbox that never mentions canRunInBackground is silent")

    # ---- the signatures arcpy calls positionally
    bad_execute = _pyt('''class Demo(object):
    def getParameterInfo(self):
        return []

    def execute(self, parameters):  #@
        pass
''')
    check(_fires(bad_execute, "PYT011"),
          "execute without the messages argument fires on the def line")
    check(_silent(bad_execute.replace("def execute(self, parameters):  #@",
                                      "def execute(self, parameters, messages):  #@"),
                  "PYT011"),
          "the documented execute signature is silent")
    check(_fires(bad_execute.replace("def execute(self, parameters):  #@",
                                     "def execute(self, params, msgs):  #@"), "PYT011"),
          "execute with the right arity and the wrong names still fires")

    bad_update = _pyt('''class Demo(object):
    def getParameterInfo(self):
        return []

    def updateMessages(self, parameters, messages):  #@
        pass
''')
    check(_fires(bad_update, "PYT016"),
          "updateMessages with an extra argument fires on the def line")
    check(_silent(bad_update.replace("def updateMessages(self, parameters, messages):  #@",
                                     "def updateMessages(self, parameters):  #@"),
                  "PYT016"),
          "the documented updateMessages signature is silent")

    # ---- the Toolbox class itself
    no_toolbox = '''import arcpy


class Demo(object):
    def isLicensed(self):
        return True
'''
    check(_lines_for(no_toolbox, "PYT012") == [1],
          "a file with no Toolbox class fires once, on line 1")
    check(_silent(CLEAN, "PYT012"),
          "a file that defines Toolbox is silent")
    check(any(f.code == "PYT009" for f in check_source(no_toolbox)),
          "the other classes are still linted when Toolbox is missing")

    no_tools = '''import arcpy


class Toolbox(object):  #@
    def __init__(self):
        self.label = "t"
        self.alias = "t"
        self.description = "t"
'''
    check(_fires(no_tools, "PYT013"),
          "a Toolbox that never assigns self.tools fires on the class line")
    check(_silent(CLEAN, "PYT013"),
          "a Toolbox that assigns self.tools is silent")
    check(_lines_for(CLEAN.replace('        self.alias = "demo"\n', ""),
                     "PYT018") == [4],
          "a Toolbox with no self.alias fires on the class line")
    check(_silent(CLEAN, "PYT018"),
          "a Toolbox that assigns self.alias is silent")

    # ---- getParameterInfo that returns nothing
    no_return = _pyt('''class Demo(object):
    def getParameterInfo(self):  #@
        params = []
        params.append(arcpy.Parameter(name="a", parameterType="Required",
                                      direction="Input"))
''')
    check(_fires(no_return, "PYT014"),
          "getParameterInfo with no return fires on the def line")
    check(_silent(no_return + "        return params\n", "PYT014"),
          "getParameterInfo that returns the list it built is silent")

    # ---- values arcpy does not document
    typo = _pyt('''class Demo(object):
    def getParameterInfo(self):
        p0 = arcpy.Parameter(name="a", parameterType="Require", direction="Input")  #@
        return [p0]
''')
    check(_fires(typo, "PYT015"),
          "a misspelled parameterType fires on the Parameter line")
    check(_silent(typo.replace('parameterType="Require"', 'parameterType="Required"'),
                  "PYT015"),
          "a documented parameterType is silent")
    check(_fires(typo.replace('parameterType="Require", direction="Input"',
                              'parameterType="Required", direction="In"'), "PYT015"),
          "a misspelled direction fires on the Parameter line")
    check(_silent(typo.replace('parameterType="Require"', "parameterType=kind"),
                  "PYT015"),
          "a parameterType built at run time is not guessed at")

    # ---- a file that does not parse at all
    broken = "class Toolbox(object)\n    pass\n"
    check([f.code for f in check_source(broken)] == ["PYT000"],
          "a file that does not parse reports one finding and stops")
    check(check_source(broken)[0].line == 1,
          "the syntax finding carries the line the parser failed on")
    check(check_source("\ufeff" + CLEAN) == [],
          "a leading UTF-8 byte order mark is not a syntax error  <-- pinned defect")
    check(_lines_for("\ufeff" + broken, "PYT000") == [1],
          "a real syntax error behind a byte order mark still reports line 1")

    # ---- selecting and ignoring codes
    findings = check_source(bad_index)
    check(len(findings) > 1, "the index example trips other rules as well")
    check([f.code for f in filter_findings(findings, select={"PYT001"})] == ["PYT001"],
          "--select keeps only the named code")
    without = filter_findings(findings, ignore={"PYT001"})
    check(all(f.code != "PYT001" for f in without), "--ignore drops the named code")
    check(len(without) == len(findings) - 1, "--ignore drops nothing else")
    check(filter_findings(findings, select={"PYT001"}, ignore={"PYT001"}) == [],
          "--ignore wins over --select for the same code")
    check(filter_findings(findings) == findings, "no filter keeps everything")

    # ---- the exit code
    check(exit_code([]) == 0, "a file with no findings exits 0")
    check(exit_code(check_source(CLEAN)) == 0, "a correct toolbox exits 0")
    check(exit_code(check_source(bad_index)) == 1, "an error-severity finding exits 1")
    warnings_only = [f for f in check_source(no_licence) if f.severity == "warning"]
    check(bool(warnings_only) and exit_code(warnings_only) == 0,
          "warnings on their own exit 0")
    check(exit_code(warnings_only, strict=True) == 1, "--strict makes a warning exit 1")

    # ---- ordering and rendering
    lines = [f.line for f in check_source(bad_index)]
    check(lines == sorted(lines), "findings come out in line order")
    rendered = format_finding(check_source(bad_index)[0], "MyTools.pyt")
    check(rendered.startswith("MyTools.pyt:"), "a rendered line starts with the path")
    check(rendered.split(":")[1].isdigit(), "a rendered line carries the line number")
    check(" error: " in rendered or " warning: " in rendered,
          "a rendered line carries the severity")
    check(len(set(ALL_CODES)) == len(ALL_CODES), "no rule code is used twice")
    check(all(RULE_SEVERITY[code] in ("error", "warning") for code in ALL_CODES),
          "every rule is an error or a warning and nothing else")

    # ---- code lists
    check(parse_codes("PYT001,PYT006", "--select") == {"PYT001", "PYT006"},
          "a comma separated code list parses")
    check(parse_codes("pyt001", "--select") == {"PYT001"},
          "a lower case code is accepted")
    check(parse_codes("", "--select") == set(), "an empty code list selects nothing")
    raises(lambda: parse_codes("PYT999", "--select"), "an unknown code raises")

    # ---- argument handling
    args = _parse(["MyTools.pyt"])
    check(args.paths == ["MyTools.pyt"], "a path is read positionally")
    check(args.json is False, "--json defaults to OFF")
    check(args.strict is False, "--strict defaults to OFF")
    check(args.self_test is False, "--self-test defaults to OFF")
    check(args.list_rules is False, "--list-rules defaults to OFF")
    check(args.select is None, "--select defaults to nothing selected")
    check(args.ignore is None, "--ignore defaults to nothing ignored")
    check(_parse(["a.pyt", "--json"]).json is True, "--json is read")
    check(_parse(["a.pyt", "--strict"]).strict is True, "--strict is read")
    check(_parse(["a.pyt", "--select", "PYT001"]).select == "PYT001", "--select is read")
    check(_parse(["a.pyt", "--ignore", "PYT007"]).ignore == "PYT007", "--ignore is read")
    check(_parse(["--self-test"]).self_test is True, "--self-test is read")
    check(_parse(["--list-rules"]).list_rules is True, "--list-rules is read")
    check(_parse(["a.pyt", "b.pyt"]).paths == ["a.pyt", "b.pyt"],
          "more than one path is read")
    check(_parse([]).paths == [], "no path at all parses, and main turns it into a usage error")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for item in failed:
            print("  FAILED: %s" % item)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ------------------------------------------------------------------------ cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="pytlint.py",
        description="Static analysis for ArcGIS Python toolboxes. Reads the "
                    "file with ast and never imports it.",
        epilog="This tool only reads. It never edits a toolbox, so there is "
               "nothing here to guard behind --apply.",
    )
    ap.add_argument("paths", nargs="*", help=".pyt files to check")
    ap.add_argument("--json", action="store_true",
                    help="machine readable output on stdout")
    ap.add_argument("--select",
                    help="comma separated codes to report, to the exclusion of "
                         "every other code")
    ap.add_argument("--ignore",
                    help="comma separated codes to drop. Applied after --select.")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on a warning as well as on an error. Off by "
                         "default, so a warning never breaks a build that did "
                         "not ask for it.")
    ap.add_argument("--list-rules", dest="list_rules", action="store_true",
                    help="print every rule code and exit")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if args.list_rules:
        for code, severity, text in RULES:
            print("%s  %-7s  %s" % (code, severity, text))
        return 0

    if not args.paths:
        print("error: give at least one .pyt path. Use --self-test to verify "
              "the tool without a toolbox.", file=sys.stderr)
        return 64

    try:
        select = parse_codes(args.select, "--select")
        ignore = parse_codes(args.ignore, "--ignore")
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64

    status = 0
    collected = []
    for raw in args.paths:
        path = Path(raw)
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            print("error: cannot read %s: %s" % (path, exc), file=sys.stderr)
            status = max(status, 2)
            continue

        findings = filter_findings(check_source(source), select, ignore)
        collected.append((path, findings))
        status = max(status, exit_code(findings, args.strict))

    if args.json:
        rows = []
        for path, findings in collected:
            rows.extend(f.as_dict(path) for f in findings)
        print(json.dumps({
            "findings": rows,
            "errors": len([r for r in rows if r["severity"] == "error"]),
            "warnings": len([r for r in rows if r["severity"] == "warning"]),
        }, indent=2))
        return status

    printed = 0
    for path, findings in collected:
        for finding in findings:
            print(format_finding(finding, path))
            printed += 1
    if printed == 0 and collected:
        print("%d file(s) checked, nothing to report." % len(collected))
    return status


if __name__ == "__main__":
    sys.exit(main())
