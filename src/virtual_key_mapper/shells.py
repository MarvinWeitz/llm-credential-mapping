"""Shell snippets: export or unset nonsecret variables, and a hook that refreshes them per directory.

Only attribution values are exported, never a credential. Values are quoted per shell; they may
contain quotes (spend-log metadata is JSON).
"""

import shlex

SHELLS = ("bash", "zsh", "fish", "pwsh")


def _single_quoted(value: str, shell: str) -> str:
    if shell == "pwsh":
        return "'" + value.replace("'", "''") + "'"
    if shell == "fish":
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return shlex.quote(value)


def assignments(shell: str, values: dict[str, str | None]) -> str:
    """Statements that set each variable to its value, or unset it when the value is None."""
    lines = []
    for name, value in values.items():
        if shell == "pwsh":
            lines.append(
                f"$env:{name} = {_single_quoted(value, shell)}" if value is not None
                else f"Remove-Item -Path Env:{name} -ErrorAction SilentlyContinue"
            )
        elif shell == "fish":
            lines.append(f"set -gx {name} {_single_quoted(value, shell)}" if value is not None else f"set -e {name}")
        else:
            lines.append(f"export {name}={_single_quoted(value, shell)}" if value is not None else f"unset {name}")
    return "\n".join(lines)


def hook(shell: str, argv: list[str]) -> str:
    """A snippet for the shell profile that runs ``argv + ["--directory", <current directory>]`` on directory change.

    ``argv`` must print ``assignments`` for the same shell.
    """
    if shell == "pwsh":
        command = "& " + " ".join(_single_quoted(arg, shell) for arg in argv)
        return "\n".join([
            "# vkm: set the customer for tools started from this directory.",
            "$global:__VkmDirectory = $null",
            "# Loading the profile again must not wrap the wrapper (it would call itself).",
            "if (\"$function:prompt\" -notmatch '__VkmPrompt') { $global:__VkmPrompt = $function:prompt }",
            "function global:prompt {",
            "    if ($PWD.Path -ne $global:__VkmDirectory) {",
            "        $global:__VkmDirectory = $PWD.Path",
            f"        ({command} '--directory' $PWD.Path) | Out-String | Invoke-Expression",
            "    }",
            "    & $global:__VkmPrompt",
            "}",
        ])
    command = " ".join(shlex.quote(arg) for arg in argv)
    if shell == "fish":
        return "\n".join([
            "# vkm: set the customer for tools started from this directory.",
            "function __vkm_hook --on-variable PWD",
            f'    {command} --directory "$PWD" | source',
            "end",
            "__vkm_hook",
        ])
    if shell == "zsh":
        return "\n".join([
            "# vkm: set the customer for tools started from this directory.",
            f'__vkm_hook() {{ eval "$({command} --directory "$PWD")"; }}',
            "autoload -Uz add-zsh-hook",
            "add-zsh-hook chpwd __vkm_hook",
            "__vkm_hook",
        ])
    return "\n".join([
        "# vkm: set the customer for tools started from this directory.",
        "__vkm_hook() {",
        "    local status=$?  # Keep $? for prompt commands that run after this one.",
        '    if [ "$PWD" != "${__vkm_directory-}" ]; then',
        '        __vkm_directory="$PWD"',
        f'        eval "$({command} --directory "$PWD")"',
        "    fi",
        '    return "$status"',
        "}",
        'case ";${PROMPT_COMMAND-};" in',
        '    *";__vkm_hook;"*) ;;',
        '    *) PROMPT_COMMAND="__vkm_hook${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;',
        "esac",
    ])


def without_daemon(shell: str, tool: str = "codex") -> str:
    """Make ``tool`` start ``--no-daemon``: a shared daemon would keep another terminal's customer variables."""
    if shell == "pwsh":
        return "\n".join([
            f"function global:{tool} {{",
            f"    & (Get-Command {tool} -CommandType Application | Select-Object -First 1) --no-daemon @args",
            "}",
        ])
    if shell == "fish":
        return f"function {tool} --wraps {tool}\n    command {tool} --no-daemon $argv\nend"
    return f'{tool}() {{ command {tool} --no-daemon "$@"; }}'


def pwsh_loader(argv: list[str]) -> list[str]:
    """PowerShell arguments that load the hook printed by ``argv`` without any script file.

    For machines whose execution policy blocks profiles and other .ps1 files: the policy does not
    apply to commands passed with -Command, so a terminal can load the hook when it starts.
    """
    command = "& " + " ".join(_single_quoted(arg, "pwsh") for arg in argv) + " | Out-String | Invoke-Expression"
    return ["-NoExit", "-Command", command]
