// Caches the CLI's token: until 60 s before its expiry, and at most 4 minutes so a renewal or sign-in is
// picked up soon. Concurrent requests share one CLI call.
import type { CliToken } from "./cli";

export const MAX_CACHE_SECONDS = 240;
export const MINIMUM_VALIDITY_SECONDS = 60;

export class TokenCache {
  private cached: { token: CliToken; until: number } | undefined;
  private pending: Promise<CliToken> | undefined;

  constructor(private readonly fetch: () => Promise<CliToken>, private readonly now: () => number = () => Date.now() / 1000) {}

  async get(): Promise<string> {
    if (this.cached && this.now() < this.cached.until) return this.cached.token.access_token;
    this.pending ??= this.fetch().finally(() => (this.pending = undefined));
    const token = await this.pending;
    const fetched = this.now();
    const expiry = token.expires_at === null ? Infinity : token.expires_at - MINIMUM_VALIDITY_SECONDS;
    this.cached = { token, until: Math.min(fetched + MAX_CACHE_SECONDS, expiry) };
    return token.access_token;
  }

  /** Forget the token, e.g. after the gateway answered 401. */
  invalidate(): void {
    this.cached = undefined;
  }
}
