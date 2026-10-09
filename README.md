# pytlint

Static analysis for ArcGIS Python toolboxes. Names the parameter index that will grey out the
dialog and the AddError that reports success. Never imports the file it checks.

A `.pyt` works on your machine. A colleague opens it in Pro, the dialog will not open, and there
is no traceback and no message. The cause is two methods that disagree about how many parameters
the tool has:

```python
def getParameterInfo(self):
    return [parcels, where, out_fc]     # three parameters

def updateMessages(self, parameters):
    if parameters[3].valueAsText:       # IndexError, swallowed by Pro
        parameters[3].setErrorMessage("pick a workspace")
```

The second flavour is worse, because it succeeds. `execute` calls `arcpy.AddError(...)` and then
returns instead of raising. The tool prints red text, reports "Completed successfully", and the
scheduled task wrapping it exits 0 forever. Nobody looks at a job that keeps passing.

The third is the quietest of all. `arcpy` enumerates only static, module-level tool classes,
so a `self.tools` built by a comprehension or a factory call makes the toolbox open **empty** in
Pro. No error, no message, no traceback, and every tool in the file is gone.

```python
class Toolbox(object):
    def __init__(self):
        self.tools = [_make(check) for check in CHECKS]   # the toolbox opens empty
```

```
$ python pytlint.py --self-test
pytlint self-test: no arcpy, no toolbox file, no network
--------------------------------------------------------------------
PASS  a correct toolbox produces no findings at all
PASS  an index past the returned parameter count fires on the reading line
PASS  the last valid index is silent
PASS  the message names both the index used and the count returned
PASS  the message names the highest index that would have worked
PASS  an index exactly one past the end fires  <-- pinned defect
...
PASS  a Parameter built but never returned does not raise the count  <-- pinned defect
PASS  a list built by append in a loop is not counted at all  <-- pinned defect
PASS  parameters built by a helper call are not counted  <-- pinned defect
PASS  a class with no getParameterInfo reports no index overrun  <-- pinned defect
...
PASS  a class named twice in self.tools is linted once  <-- pinned defect
...
PASS  a leading UTF-8 byte order mark is not a syntax error  <-- pinned defect
...
PASS  the message quotes the signature the file really has  <-- pinned defect
PASS  a keyword-only argument is quoted as keyword-only  <-- pinned defect
PASS  a positional-only execute is the signature arcpy calls and is silent  <-- pinned defect
...
PASS  a list grown with += is not counted either  <-- pinned defect
...
PASS  self.tools built by a factory call fires on the assignment  <-- pinned defect
PASS  one dynamic self.tools does not hide every finding in the rest of the file behind it  <-- pinned defect
PASS  a broad except that reports no traceback fires on the handler
PASS  an execute that hands the work to a shared wrapper holds no handler
PASS  the path in the message is the path, not a repr of it with every backslash doubled  <-- pinned defect
...
PASS  a unique prefix of --list-rules is refused by the parser  <-- pinned defect
...
PASS  a path that is not there exits 2
PASS  a directory instead of a file exits 2
PASS  a file that is not text at all exits 2
PASS  a syntax error reports one finding and stops, it does not crash
PASS  no module-level line of the toolbox ran  <-- the headline claim
PASS  the JSON document carries exactly the three documented keys
PASS  every JSON finding carries the five documented keys
...
PASS  a toolbox saved with a byte order mark still reads as clean
--------------------------------------------------------------------
254 assertions, 0 failed
```

The `...` above stands for the assertions not quoted here. Every line that is quoted is printed
verbatim, in that order, by the command above.

## Requirements

Python 3.9 or later. Standard library only: `ast`, `argparse`, `json`, `sys`, `pathlib`, and
`contextlib`, `io`, `os` and `tempfile` for the self-test. Nothing to install.

The toolbox is parsed, never imported, so `arcpy` is not needed and no ArcGIS software has to be
present. The same command works in ArcGIS Pro's Python and in a plain `python3` on a build agent.

`--self-test` needs no `arcpy`, no network and no toolbox of your own. It lints toolbox sources it
carries inside it, and for the file handling it writes a handful of files into a temporary
directory and removes them again. One of those files raises at module level: if anything ever
imported a toolbox instead of parsing it, that assertion is the one that fails.

```
git clone https://github.com/uhsear/pytlint.git
```

## Quick start

```
python pytlint.py --self-test
python pytlint.py MyTools.pyt
```

## Usage

Output is one compiler-shaped line per finding, so an editor can jump to it.

```
$ python pytlint.py ParcelTools.pyt
ParcelTools.pyt:11: PYT008 error: self.tools names Ghost, which is not defined in this file. Pro fails to load the whole toolbox, not just that one tool.
ParcelTools.pyt:14: PYT007 warning: RebuildCentroids has no isLicensed. The tool then stays enabled whatever extension it needs, and fails at run time instead of greying out.
ParcelTools.pyt:18: PYT017 warning: self.canRunInBackground is an ArcMap setting. Pro ignores it, so this line promises something it does not do.
ParcelTools.pyt:27: PYT003 warning: Required parameter 'out_fc' follows the Optional one at line 24. The dialog lists optional parameters last, so the order the user sees is not this one.
ParcelTools.pyt:27: PYT005 warning: parameter 'out_fc' is direction=Output but execute never assigns parameters[2]. Anything downstream in a model receives an empty result.
ParcelTools.pyt:37: PYT001 error: RebuildCentroids.updateMessages reads parameters[3] but getParameterInfo returns 3 parameter(s), so the highest valid index is 2. Pro swallows the IndexError and the dialog will not open.
ParcelTools.pyt:38: PYT001 error: RebuildCentroids.updateMessages reads parameters[3] but getParameterInfo returns 3 parameter(s), so the highest valid index is 2. Pro swallows the IndexError and the dialog will not open.
ParcelTools.pyt:39: PYT004 warning: updateMessages assigns .value. Pro has already read the values by this point and discards the change. Set a value in updateParameters instead.
ParcelTools.pyt:44: PYT006 error: execute reports an error at line 43 and then returns. The tool prints red text and still reports success, so the scheduled task wrapping it exits 0. Raise arcpy.ExecuteError instead.
ParcelTools.pyt:48: PYT009 error: ExportSales has no __init__. arcpy calls it on every tool.
ParcelTools.pyt:48: PYT010 warning: ExportSales never assigns self.label, so the dialog shows the class name
ParcelTools.pyt:48: PYT010 warning: ExportSales never assigns self.description, so the help pane is empty
ParcelTools.pyt:52: PYT002 error: ExportSales declares the parameter name 'year' twice, first at line 50. Every lookup by that name reaches one of them and the other is unreachable.
ParcelTools.pyt:52: PYT015 error: parameterType='Require' is not one of Required, Optional, Derived
ParcelTools.pyt:59: PYT011 error: execute(self, parameters) does not match execute(self, parameters, messages), which is the signature arcpy calls
```

| Flag | Default | What it does |
|---|---|---|
| `paths` | none | One or more `.pyt` files. Required unless `--self-test` or `--list-rules`. |
| `--json` | off | Machine readable output on stdout. |
| `--select` | none | Comma separated codes to report, to the exclusion of every other code. |
| `--ignore` | none | Comma separated codes to drop. Applied after `--select`. |
| `--strict` | off | Exit 1 on a warning as well as on an error. |
| `--list-rules` | off | Print every rule code and exit. |
| `--self-test` | off | Run the offline assertions and exit. |

Two useful shapes. The first fails a build on the rules that break a tool. The second reports
everything without failing anything:

```
python pytlint.py toolboxes/*.pyt --select PYT001,PYT002,PYT006
python pytlint.py toolboxes/*.pyt --ignore PYT007,PYT017 --json
```

This tool only reads. It never edits a toolbox, so there is nothing here to guard behind
`--apply`.

## What it checks

| Code | Severity | Rule |
|---|---|---|
| `PYT000` | error | The file does not parse as Python. |
| `PYT001` | error | A parameter index is past the end of what `getParameterInfo` returns. |
| `PYT002` | error | Two parameters in one tool share a `name`. |
| `PYT003` | warning | A `Required` parameter is declared after an `Optional` one. |
| `PYT004` | warning | `updateMessages` assigns `.value`, which Pro discards. |
| `PYT005` | warning | An `Output` parameter is never written in `execute`. |
| `PYT006` | error | `AddError` is followed by a bare `return` instead of a `raise`. |
| `PYT007` | warning | A tool class has no `isLicensed`. |
| `PYT008` | error | `self.tools` names a class that is not defined in this file. |
| `PYT009` | error | A tool class is missing `__init__`, `getParameterInfo` or `execute`. |
| `PYT010` | warning | A class has no `self.label` or no `self.description`. |
| `PYT011` | error | `execute` does not match `(self, parameters, messages)`. |
| `PYT012` | error | The file defines no `Toolbox` class. |
| `PYT013` | error | The `Toolbox` class never assigns `self.tools`. |
| `PYT014` | error | `getParameterInfo` returns nothing. |
| `PYT015` | error | A `parameterType` or `direction` value is not a documented one. |
| `PYT016` | error | `updateParameters` or `updateMessages` does not match `(self, parameters)`. |
| `PYT017` | warning | `self.canRunInBackground` is set, and Pro ignores it. |
| `PYT018` | warning | The `Toolbox` class has no `self.alias`. |
| `PYT019` | error | `self.tools` is built while the toolbox loads, so Pro opens it empty. |
| `PYT020` | error | `execute` catches every exception and neither re-raises nor reports the traceback. |
| `PYT021` | warning | `sys.path` is given one absolute path instead of a local-first fallback. |

A rule is an error only when the finding is certain breakage. Everything else is a warning and
does not change the exit code, unless you pass `--strict`.

## Exit codes

0 clean, 1 an error-severity finding, 2 a file could not be read, 64 usage error.

## Why it parses instead of importing

Importing a `.pyt` to inspect it runs the module-level code in it and needs `arcpy` on the path.
That is a database connection opened at import time, a licence checked out, and a build agent
that has to carry ArcGIS Pro. Parsing with `ast` runs none of the file, so pytlint works in CI on
a machine with no Esri software on it.

## What pylint and flake8 already do

Run them too. They are better Python linters than this will ever be, and they catch the unused
import, the shadowed name and the undefined variable that pytlint says nothing about.

They do not know what a `.pyt` is. `parameters[4]` is a valid subscript of a function argument,
and no general linter can know the length is decided by a different method in the same class. A
bare `return` after `arcpy.AddError` is also ordinary Python. The gap pytlint fills is that small
set of patterns where correct Python is a broken geoprocessing tool.

The ArcGIS-specific alternative is to open the toolbox in Pro and click every tool, which needs
Pro, a licence, and a person.

## Limits

- Only literal integer indices are checked. `parameters[i]` and `parameters[self.IDX]` are
  skipped, because guessing at a computed index produces false reports.
- Only literal keyword values are read. `parameterType=kind` is skipped for the same reason.
- The parameter count comes from what `getParameterInfo` returns. A literal list works, and so
  does the Esri template shape `params = [param0, param1]` followed by `return params`. A list
  filled by `params.append(...)` in a loop, a `return self._build()`, or a missing
  `getParameterInfo` leaves the count unknown, and then `PYT001` and `PYT005` are skipped for
  that tool. Counting the `arcpy.Parameter(...)` calls instead reported working tools as broken,
  which is worse than the miss.
- `PYT001` reads the index, not the flow. It does not know that `parameters[3]` sits behind an
  `if` that never runs.
- `PYT006` only sees an `AddError` and a bare `return` in the same block. An `AddError` in an
  `if` body followed by a `return` further out, or an `execute` that simply runs off its last
  line after `AddError`, is the same defect and is not reported.
- One file at a time. A tool class imported from another module is invisible, and `PYT008`
  reports it as undefined.
- `PYT005` looks for an assignment to `parameters[i]` or a call to `SetParameter`. A tool that
  writes its output through a helper function is reported even though it is correct.
- `PYT019` reads the shape of `self.tools`, not what the shape evaluates to. A list of plain
  module-level class names is accepted, and everything else is reported, so a factory that
  happens to return static classes is a false report. Pro accepts only the plain list, which is
  why the rule is written the way Pro reads the file.
- `PYT020` reads the handlers inside `execute` only. A broad `except` in a helper that `execute`
  calls is the same defect and is not reported, and an `execute` that hands its work to a shared
  wrapper is silent because the handler is then in the wrapper.
- `PYT021` reads literal paths only. `sys.path.insert(0, os.path.join(ROOT, "shared"))` is
  skipped, because the value of `ROOT` is not decidable from the source.
- No datatype checking. A `datatype` string that Pro does not recognise is not caught.
- A clean run is not a working toolbox. This finds structural mistakes, not wrong geoprocessing.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [arcade-rule-deploy](https://github.com/uhsear/arcade-rule-deploy) - the Arcade equivalent, checked before it reaches a geodatabase
- [jobharness](https://github.com/uhsear/jobharness) - what the toolbox should run under when it is scheduled
