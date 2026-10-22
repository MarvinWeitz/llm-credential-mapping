# Virtual Key Mapper: local sign-in

Install this in your **local** VS Code (it always runs on your own computer, also for dev
container and SSH windows). It lets the Virtual Key Mapper extension in a remote window use the
sign-in of your local `vkm` CLI:

- the remote side only ever receives short-lived access tokens, on request;
- the refresh grant stays in your local OS vault;
- no background service, no network port: requests travel through VS Code itself;
- the remote window's profile is created from your local one, including its own label
  definitions and output overrides and a central rules URL (central rules in a local file
  cannot be copied; the remote window then warns that the organisation's label definitions
  are missing there).

Settings (in your local user settings): `vkm.localCommand`, how to run the local CLI,
e.g. `["C:\\path\\to\\venv\\Scripts\\python.exe", "-m", "virtual_key_mapper"]` (empty: the
`vkm.command` user setting, else `vkm` from PATH); and
`vkm.localProfiles`, the local profiles remote windows may use (empty: only the
default profile; the first entry is used when a window names none). Requests for any other
profile are refused, whatever the container or workspace asks for.

"Virtual Key Mapper: Sign in on this computer" runs `vkm login` locally; remote
windows offer it when the local sign-in is missing or expired.
