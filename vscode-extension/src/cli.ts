import { spawn } from "node:child_process";

export interface Profile {
  name: string;
  gateway: string;
  team_id: string;
  allow_http: boolean;
}

export interface CliRuntime {
  executable: string;
  args: string[];
  home: string;
  dbusAddress: string;
}

export class IntegrationError extends Error {}

export function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseProfiles(value: unknown): Profile[] {
  if (!record(value) || value.version !== 1 || !Array.isArray(value.projects)) {
    throw new IntegrationError("Invalid CLI project response. Update the Python CLI.");
  }
  const names = new Set<string>();
  return value.projects.map((item: unknown) => {
    if (!record(item) || typeof item.name !== "string"
      || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(item.name)
      || typeof item.gateway !== "string" || typeof item.team_id !== "string"
      || !item.team_id.trim() || item.team_id !== item.team_id.trim() || item.team_id.length > 256
      || /[\x00-\x1f]/.test(item.team_id) || /[\s\x00-\x1f]/.test(item.gateway)
      || typeof item.allow_http !== "boolean" || names.has(item.name)) {
      throw new IntegrationError("Invalid CLI project metadata.");
    }
    let url: URL;
    try {
      url = new URL(item.gateway);
    } catch {
      throw new IntegrationError("Invalid CLI gateway URL.");
    }
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    if (url.username || url.password || url.search || url.hash || url.pathname !== "/"
      || (url.protocol !== "https:" && !(url.protocol === "http:" && item.allow_http && loopback))) {
      throw new IntegrationError("CLI gateway must be a trusted HTTPS root (or opted-in HTTP loopback).");
    }
    names.add(item.name);
    return { name: item.name, gateway: url.origin, team_id: item.team_id, allow_http: item.allow_http };
  });
}

export class Cli {
  constructor(
    readonly runtime: CliRuntime,
    private readonly warn: (message: string) => void = (message) => console.warn(message),
  ) {}

  arguments(args: string[]): string[] {
    return [...this.runtime.args, ...(this.runtime.home ? ["--home", this.runtime.home] : []), ...args];
  }

  environment(): NodeJS.ProcessEnv {
    return {
      ...process.env,
      ...(this.runtime.dbusAddress ? { DBUS_SESSION_BUS_ADDRESS: this.runtime.dbusAddress } : {}),
    };
  }

  async run(args: string[], signal?: AbortSignal): Promise<string> {
    signal?.throwIfAborted();
    return new Promise((resolve, reject) => {
      const child = spawn(this.runtime.executable, this.arguments(args), {
        env: this.environment(), shell: false, windowsHide: true, stdio: ["ignore", "pipe", "pipe"],
      });
      let stdout = "";
      let size = 0;
      let failed = false;
      let warning = false;
      const fail = (error: Error) => {
        if (failed) return;
        failed = true;
        child.kill();
        reject(error);
      };
      const cancel = () => fail(new IntegrationError("Credential operation cancelled."));
      signal?.addEventListener("abort", cancel, { once: true });
      const timer = setTimeout(() => fail(new IntegrationError("Credential CLI timed out.")), 30_000);
      child.stdout.on("data", (data: Buffer) => {
        size += data.length;
        if (size > 1_048_576) fail(new IntegrationError("Credential CLI output exceeded its limit."));
        else stdout += data.toString("utf8");
      });
      // CLI stderr can include authentication URLs or an untrusted executable's output.
      child.stderr.on("data", (data: Buffer) => {
        warning = true;
        size += data.length;
        if (size > 1_048_576) fail(new IntegrationError("Credential CLI output exceeded its limit."));
      });
      child.on("error", () => fail(new IntegrationError("Cannot start the credential CLI. Configure its executable on the workspace host.")));
      child.on("close", (code) => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", cancel);
        if (failed) return;
        if (code !== 0) {
          reject(new IntegrationError("Credential CLI failed. Run 'status --project NAME' on the same host; check login and OS vault/D-Bus access."));
        } else {
          if (warning) this.warn("Credential CLI reported a warning. Inspect it with CLI status on the same host; raw output is not logged here.");
          resolve(stdout);
        }
      });
    });
  }

  async profiles(signal?: AbortSignal): Promise<Profile[]> {
    const output = await this.run(["project", "list", "--json"], signal);
    let value: unknown;
    try { value = JSON.parse(output); }
    catch { throw new IntegrationError("Credential CLI did not return JSON. Update the Python CLI."); }
    return parseProfiles(value);
  }

  async profile(name: string, signal?: AbortSignal): Promise<Profile> {
    const profile = (await this.profiles(signal)).find((item) => item.name === name);
    if (!profile) throw new IntegrationError("The workspace project is no longer configured in the CLI.");
    return profile;
  }

  async token(profile: Profile, signal?: AbortSignal): Promise<string> {
    const output = await this.run(["token", "--project", profile.name], signal);
    const token = output.replace(/\r?\n$/, "");
    if (!token || token.length > 65_536 || /[\s\x00-\x1f\x7f]/.test(token)) {
      throw new IntegrationError("Invalid credential-helper output. Tokens must be the only stdout content.");
    }
    return token;
  }
}
