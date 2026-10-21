// Calls the vkm CLI. Its JSON outputs are the extension's only source of credentials, endpoints and
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
  if (!command.length) throw new CliError("Set vkm.command to the vkm CLI.", false, false);
  return (args) =>
    new Promise((resolve, reject) => {
      execFile(command[0], [...command.slice(1), ...args], { windowsHide: true, maxBuffer: 4 << 20, timeout: 60000 },
        (error, stdout, stderr) => {
          if (!error) return resolve(stdout);
          const notFound = (error as NodeJS.ErrnoException).code === "ENOENT";
          const message = notFound
            ? `Cannot run ${command[0]}; set vkm.command to the vkm CLI.`
            : String(stderr || error.message).replace(/^Error: /, "").trim();
          reject(new CliError(message, /\blogin\b/.test(message), /No customer is assigned/.test(message)));
        });
    });
}

function parse<T>(output: string, what: string): T {
  try {
    return JSON.parse(output) as T;
  } catch {
    throw new CliError(`vkm returned an unreadable ${what}; update the CLI.`, false, false);
  }
}

/** The selected profile, or the CLI's default when empty. */
const option = (name: string) => (name ? ["--profile", name] : []);

export async function token(run: Runner, profileName: string): Promise<CliToken> {
  const value = parse<CliToken>(await run(["token", "--json", ...option(profileName)]), "token");
  if (typeof value.access_token !== "string" || !value.access_token) throw new CliError("vkm returned no token.", true, false);
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

export interface LocalProfile {
  name: string;
  gateway: { type: string; url: string };
  rules?: string | null;
}

/**
 * Arguments to create the container's profile from the local one, signing in through the relay. Central rules are
 * copied when they are a URL; a local file path does not exist in the container, so it is returned to warn about.
 */
export function remoteProfileArgs(local: LocalProfile): { args: string[]; skippedRules: string | null } {
  const insecure = local.gateway.url.startsWith("http:");
  const rules = typeof local.rules === "string" ? local.rules : null;
  const url = rules !== null && (rules.startsWith("https://") || (insecure && rules.startsWith("http://")));
  return {
    args: [
      "profile", "add", local.name, "--gateway", local.gateway.url, "--gateway-type", local.gateway.type,
      "--auth", "forwarded", "--host-profile", local.name,
      ...(insecure ? ["--allow-insecure-localhost"] : []),
      ...(url ? ["--rules", rules] : []),
    ],
    skippedRules: rules !== null && !url ? rules : null,
  };
}

/** Arguments to sign in or assign a customer interactively in a terminal. */
export const loginArgs = (profileName: string) => ["login", ...option(profileName)];
export const bindArgs = (profileName: string, directory: string) => ["repo", "bind", "--directory", directory, ...option(profileName)];
