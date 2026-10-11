// Answers relay requests from remote windows with the local llm-credentials CLI; no vscode import, so it is testable.
// Only two operations exist, and only nonsecret profile details and short-lived access tokens are returned.

export type Run = (args: string[]) => Promise<string>;

export class RunError extends Error {}

export type Answer = { ok: true; result: object } | { ok: false; error: string; login: boolean };

const PROFILE = /^([A-Za-z0-9][A-Za-z0-9._-]{0,63})?$/;

export async function answer(request: unknown, run: Run, defaultProfile: string): Promise<Answer> {
  const value = request as { operation?: unknown; profile?: unknown } | null;
  if (
    typeof value !== "object" || value === null || !["token", "profile"].includes(value.operation as string)
    || typeof value.profile !== "string" || !PROFILE.test(value.profile)
  ) {
    return { ok: false, error: "Unsupported request; update both LLM Credentials extensions.", login: false };
  }
  const profile = value.profile || defaultProfile;
  try {
    if (value.operation === "token") {
      const token = JSON.parse(await run(["token", "--json", ...(profile ? ["--profile", profile] : [])]));
      return { ok: true, result: { access_token: token.access_token, expires_at: token.expires_at ?? null, profile: token.profile, user: token.user ?? null } };
    }
    const shown = JSON.parse(await run(["profile", "show", ...(profile ? [profile] : []), "--json"]));
    return { ok: true, result: { name: shown.name, gateway: shown.gateway, endpoints: shown.endpoints } };
  } catch (error) {
    const message = error instanceof RunError ? error.message : "The local llm-credentials CLI gave an unreadable answer.";
    return { ok: false, error: message, login: /\blogin\b/.test(message) };
  }
}
