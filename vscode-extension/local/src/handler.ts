// Answers relay requests from remote windows with the local vkm CLI; no vscode import, so it is testable.
// Only two operations exist, and only nonsecret profile details and short-lived access tokens are returned.

export type Run = (args: string[]) => Promise<string>;

export class RunError extends Error {}

/** A failure names the profile to sign in to ("" for the local default). */
export type Answer = { ok: true; result: object } | { ok: false; error: string; login: boolean; profile: string };

const PROFILE = /^([A-Za-z0-9][A-Za-z0-9._-]{0,63})?$/;

const notRelayed = (name: string) => new RunError(
  `Profile '${name}' is not relayed from this computer; add it to vkm.localProfiles in your local user settings.`);

/**
 * `allowed` comes from the local user settings, so neither the container nor a workspace can widen it. Empty allows
 * only the local CLI's default profile, which is asked for without --profile, so no other profile is ever used.
 */
export async function answer(request: unknown, run: Run, allowed: string[]): Promise<Answer> {
  const value = request as { operation?: unknown; profile?: unknown } | null;
  if (
    typeof value !== "object" || value === null || !["token", "profile"].includes(value.operation as string)
    || typeof value.profile !== "string" || !PROFILE.test(value.profile)
  ) {
    return { ok: false, error: "Unsupported request; update both Virtual Key Mapper extensions.", login: false, profile: "" };
  }
  const requested = value.profile;
  const profile = allowed.length ? requested || allowed[0] : "";
  // The default's name is known only from the CLI's answer; nothing is passed on for another name.
  const check = (name: unknown) => {
    if (!allowed.length && requested && name !== requested) throw notRelayed(requested);
  };
  try {
    if (allowed.length && !allowed.includes(profile)) throw notRelayed(profile);
    if (value.operation === "token") {
      const token = JSON.parse(await run(["token", "--json", ...(profile ? ["--profile", profile] : [])]));
      check(token.profile);
      return { ok: true, result: { access_token: token.access_token, expires_at: token.expires_at ?? null, profile: token.profile, user: token.user ?? null } };
    }
    const shown = JSON.parse(await run(["profile", "show", ...(profile ? [profile] : []), "--json"]));
    check(shown.name);
    return { ok: true, result: { name: shown.name, gateway: shown.gateway, endpoints: shown.endpoints, rules: shown.rules ?? null } };
  } catch (error) {
    const message = error instanceof RunError ? error.message : "The local vkm CLI gave an unreadable answer.";
    return { ok: false, error: message, login: /\blogin\b/.test(message), profile };
  }
}
