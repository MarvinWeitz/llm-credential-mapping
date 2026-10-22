// Calls the vkm CLI. Its JSON outputs are the extension's only source of credentials, endpoints and
// label headers, so nothing here knows a gateway's paths, header names or label formats.
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

/** One defined label of a folder, in definition order; `value` is null when it has none. */
export interface CliLabel {
  key: string;
  value: string | null;
  name: string | null;
  source: string | null;
  detail: string | null;
  required: boolean;
  description: string | null;
}

/** `vkm repo show --json`: `headers` may be sent only when `complete`; otherwise `message` says why not. */
export interface CliLabels {
  labels: CliLabel[];
  missing: string[];
  ignored: Array<{ key: string; value: string | null; source: string; detail: string; reason: string }>;
  repository: string | null;
  complete: boolean;
  headers: Record<string, string>;
  message: string | null;
  notices: string[];
}

/** The CLI's fixed start of the error for required labels without a value. */
export const MISSING_PHRASE = "Required labels have no value:";

/**
 * A failed CLI call; `message` is the CLI's own error text (it never contains credentials). `directory` is the
 * folder whose labels are missing, when known.
 */
export class CliError extends Error {
  constructor(
    message: string, readonly needsLogin: boolean, readonly missingLabels: boolean, readonly directory?: string,
  ) {
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
          reject(new CliError(message, /\blogin\b/.test(message), message.includes(MISSING_PHRASE)));
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

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** A folder's labels; an answer of another shape fails, so headers are never sent from a misread answer. */
export async function labels(run: Runner, profileName: string, directory: string): Promise<CliLabels> {
  const value = parse<CliLabels>(
    await run(["repo", "show", "--json", "--directory", directory, ...option(profileName)]), "labels",
  );
  if (
    !isObject(value) || !Array.isArray(value.labels) || !value.labels.every(isObject) || !Array.isArray(value.missing)
    || typeof value.complete !== "boolean" || !isObject(value.headers)
  ) {
    throw new CliError("vkm returned unreadable labels; update the CLI.", false, false);
  }
  return value;
}

/** What the companion passes on from `vkm profile show --json`; `labels` and `outputs` are the profile's own. */
export interface LocalProfile {
  name: string;
  gateway: { type: string; url: string };
  rules?: string | null;
  labels?: unknown[];
  outputs?: Record<string, unknown>;
}

/** The keys of a format's `${KEY}` placeholders (a string or a list of strings); `$$` is a literal `$`. */
function placeholders(format: unknown): string[] {
  const entries = Array.isArray(format) ? format : [format];
  return entries.flatMap((entry) => (typeof entry === "string"
    ? [...entry.matchAll(/\$\$|\$\{([^}]*)\}/g)].flatMap((match) => (match[1] === undefined ? [] : [match[1]]))
    : []));
}

/**
 * Arguments to create the container's profile from the local one in one call, signing in through the relay, with
 * the local profile's own label definitions and outputs. Central rules are copied when they are a URL; a local file
 * path does not exist in the container, so it is returned to warn about. Without those rules, an output override
 * using a label only they define would make `profile add` fail: such overrides are left out (the gateway's default
 * applies) and named in `droppedOutputs`.
 */
export function remoteProfileArgs(
  local: LocalProfile,
): { args: string[]; skippedRules: string | null; droppedOutputs: string[] } {
  const insecure = local.gateway.url.startsWith("http:");
  const rules = typeof local.rules === "string" ? local.rules : null;
  const url = rules !== null && (rules.startsWith("https://") || (insecure && rules.startsWith("http://")));
  const skippedRules = rules !== null && !url ? rules : null;
  const definitions: Record<string, unknown> = {};
  if (Array.isArray(local.labels) && local.labels.length) definitions.labels = local.labels;
  const droppedOutputs: string[] = [];
  if (isObject(local.outputs)) {
    const own = new Set((local.labels ?? []).flatMap((label) => (isObject(label) && typeof label.key === "string" ? [label.key] : [])));
    const outputs = Object.fromEntries(Object.entries(local.outputs).filter(([name, override]) => {
      const keep = skippedRules === null || !isObject(override) || placeholders(override.format).every((key) => own.has(key));
      if (!keep) droppedOutputs.push(name);
      return keep;
    }));
    if (Object.keys(outputs).length) definitions.outputs = outputs;
  }
  return {
    args: [
      "profile", "add", local.name, "--gateway", local.gateway.url, "--gateway-type", local.gateway.type,
      "--auth", "forwarded", "--host-profile", local.name,
      ...(insecure ? ["--allow-insecure-localhost"] : []),
      ...(url ? ["--rules", rules] : []),
      ...(Object.keys(definitions).length ? [`--definitions=${JSON.stringify(definitions)}`] : []),
    ],
    skippedRules,
    droppedOutputs,
  };
}

/** Arguments to sign in, or to answer for each missing required label, interactively in a terminal. */
export const loginArgs = (profileName: string) => ["login", ...option(profileName)];
export const bindArgs = (profileName: string, directory: string) => ["repo", "bind", "--directory", directory, ...option(profileName)];
