// Calls the llm-credentials CLI. Its JSON outputs are the extension's only source of credentials, endpoints and
// attribution headers, so nothing here knows a gateway's paths or header names.
import { execFile } from "node:child_process";

export interface CliToken {
  access_token: string;
  expires_at: number | null;
  profile: string;
  user: string | null;
}

export interface CliProfile {
  name: string;
  endpoints: Record<string, string>;
}

export interface CliAttribution {
  customer: string;
  source: string;
  detail: string;
  repository: string | null;
  headers: Record<string, string>;
}

/** A failed CLI call; `message` is the CLI's own error text (it never contains credentials). */
export class CliError extends Error {
  constructor(message: string, readonly needsLogin: boolean, readonly unassigned: boolean) {
    super(message);
  }
}

export type Runner = (args: string[]) => Promise<string>;

export function runner(command: readonly string[]): Runner {
  if (!command.length) throw new CliError("Set llmCredentials.command to the llm-credentials CLI.", false, false);
  return (args) =>
    new Promise((resolve, reject) => {
      execFile(command[0], [...command.slice(1), ...args], { windowsHide: true, maxBuffer: 4 << 20, timeout: 60000 },
        (error, stdout, stderr) => {
          if (!error) return resolve(stdout);
          const notFound = (error as NodeJS.ErrnoException).code === "ENOENT";
          const message = notFound
            ? `Cannot run ${command[0]}; set llmCredentials.command to the llm-credentials CLI.`
            : String(stderr || error.message).replace(/^Error: /, "").trim();
          reject(new CliError(message, /\blogin\b/.test(message), /No customer is assigned/.test(message)));
        });
    });
}

function parse<T>(output: string, what: string): T {
  try {
    return JSON.parse(output) as T;
  } catch {
    throw new CliError(`llm-credentials returned an unreadable ${what}; update the CLI.`, false, false);
  }
}

/** The selected profile, or the CLI's default when empty. */
const option = (name: string) => (name ? ["--profile", name] : []);

export async function token(run: Runner, profileName: string): Promise<CliToken> {
  const value = parse<CliToken>(await run(["token", "--json", ...option(profileName)]), "token");
  if (typeof value.access_token !== "string" || !value.access_token) throw new CliError("llm-credentials returned no token.", true, false);
  return value;
}

export async function profile(run: Runner, profileName: string): Promise<CliProfile> {
  return parse<CliProfile>(await run(["profile", "show", ...(profileName ? [profileName] : []), "--json"]), "profile");
}

export async function attribution(run: Runner, profileName: string, directory: string): Promise<CliAttribution> {
  return parse<CliAttribution>(
    await run(["repo", "show", "--json", "--directory", directory, ...option(profileName)]), "customer",
  );
}

/** Arguments to sign in or assign a customer interactively in a terminal. */
export const loginArgs = (profileName: string) => ["login", ...option(profileName)];
export const bindArgs = (profileName: string, directory: string) => ["repo", "bind", "--directory", directory, ...option(profileName)];
