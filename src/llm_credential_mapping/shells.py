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
            "# llm-credentials: set the customer for tools started from this directory.",
            "$global:__LlmCredentialsDirectory = $null",
            "$global:__LlmCredentialsPrompt = $function:prompt",
            "function global:prompt {",
            "    if ($PWD.Path -ne $global:__LlmCredentialsDirectory) {",
            "        $global:__LlmCredentialsDirectory = $PWD.Path",
            f"        ({command} '--directory' $PWD.Path) | Out-String | Invoke-Expression",
            "    }",
            "    & $global:__LlmCredentialsPrompt",
            "}",
        ])
    command = " ".join(shlex.quote(arg) for arg in argv)
    if shell == "fish":
        return "\n".join([
            "# llm-credentials: set the customer for tools started from this directory.",
            "function __llm_credentials_hook --on-variable PWD",
            f'    {command} --directory "$PWD" | source',
            "end",
            "__llm_credentials_hook",
        ])
    if shell == "zsh":
        return "\n".join([
            "# llm-credentials: set the customer for tools started from this directory.",
            f'__llm_credentials_hook() {{ eval "$({command} --directory "$PWD")"; }}',
            "autoload -Uz add-zsh-hook",
            "add-zsh-hook chpwd __llm_credentials_hook",
            "__llm_credentials_hook",
        ])
    return "\n".join([
        "# llm-credentials: set the customer for tools started from this directory.",
        "__llm_credentials_hook() {",
        "    local status=$?  # Keep $? for prompt commands that run after this one.",
        '    if [ "$PWD" != "${__llm_credentials_directory-}" ]; then',
        '        __llm_credentials_directory="$PWD"',
        f'        eval "$({command} --directory "$PWD")"',
        "    fi",
        '    return "$status"',
        "}",
        'case ";${PROMPT_COMMAND-};" in',
        '    *";__llm_credentials_hook;"*) ;;',
        '    *) PROMPT_COMMAND="__llm_credentials_hook${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;',
        "esac",
    ])
