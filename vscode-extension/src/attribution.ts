// Which workspace folder's customer applies to a chat request, and a short cache of the CLI's answers.
import type { CliAttribution } from "./cli";

export const CACHE_MILLISECONDS = 30000;

export type Resolution = { folder: string; attribution?: CliAttribution; error?: Error };

export type Choice =
  | { kind: "folder"; folder: string; attribution: CliAttribution }
  | { kind: "choose"; folders: Resolution[] }
  | { kind: "none" };

/** Resolve each folder's customer at most every 30 s, so `repo bind` and rule changes are picked up quickly. */
export class AttributionCache {
  private readonly entries = new Map<string, { at: number; value: Promise<CliAttribution> }>();

  constructor(private readonly resolve: (folder: string) => Promise<CliAttribution>, private readonly now: () => number = Date.now) {}

  get(folder: string): Promise<CliAttribution> {
    const entry = this.entries.get(folder);
    if (entry && this.now() - entry.at < CACHE_MILLISECONDS) return entry.value;
    const value = this.resolve(folder);
    value.catch(() => this.entries.delete(folder));
    this.entries.set(folder, { at: this.now(), value });
    return value;
  }

  clear(): void {
    this.entries.clear();
  }
}

/**
 * The folder whose customer a request uses: the only folder; the active editor's folder; the folder chosen
 * earlier; or, when every folder resolves to the same customer, the first one. Otherwise the user must choose.
 */
export async function choose(
  folders: readonly string[], active: string | undefined, chosen: string | undefined, cache: AttributionCache,
): Promise<Choice> {
  if (!folders.length) return { kind: "none" };
  const single = folders.length === 1 ? folders[0] : active && folders.includes(active) ? active
    : chosen && folders.includes(chosen) ? chosen : undefined;
  if (single !== undefined) return { kind: "folder", folder: single, attribution: await cache.get(single) };
  const resolved: Resolution[] = await Promise.all(folders.map(async (folder) => {
    try {
      return { folder, attribution: await cache.get(folder) };
    } catch (error) {
      return { folder, error: error as Error };
    }
  }));
  const customers = new Set(resolved.map((entry) => entry.attribution?.customer));
  if (resolved.every((entry) => entry.attribution) && customers.size === 1) {
    return { kind: "folder", folder: resolved[0].folder, attribution: resolved[0].attribution! };
  }
  return { kind: "choose", folders: resolved };
}
