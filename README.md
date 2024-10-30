> [!WARNING]
> Work in progress

**Smarts** is a [Language Server Protocol](https://microsoft.github.io/language-server-protocol/) client for [Sublime Text](https://www.sublimetext.com).


## Development

### [Project Python versions](https://docs.astral.sh/uv/concepts/python-versions/#project-python-versions)

By default uv python install will verify that a managed Python version is installed or install the latest version.

However, a project may include a .python-version file specifying a default Python version. If present, uv will install the Python version listed in the file.

### [Finding a Python executable](https://docs.astral.sh/uv/concepts/python-versions/#finding-a-python-executable)

To find a Python executable, use the uv python find command:

```
$ uv python find
```
