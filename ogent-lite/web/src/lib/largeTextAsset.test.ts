import { describe, expect, it } from "vitest";

import {
  LargeTextAssetError,
  planLargeTextAsset,
} from "./largeTextAsset";

describe("planLargeTextAsset", () => {
  it("keeps a 200,000-character turn inline", () => {
    expect(planLargeTextAsset("x".repeat(200_000))).toBeNull();
  });

  it("creates a deterministic, lossless asset for larger pasted text", () => {
    const text = `Task heading\n${"é".repeat(200_000)}`;
    const first = planLargeTextAsset(text);
    const second = planLargeTextAsset(text);

    expect(first).not.toBeNull();
    expect(first?.text).toBe(text);
    expect(first?.byteLength).toBe(new TextEncoder().encode(text).byteLength);
    expect(first?.filename).toBe(second?.filename);
    expect(first?.prompt.length).toBeLessThan(200_000);
    expect(first?.prompt).toContain(first?.filename);
  });

  it("fails before upload when the configured asset quota is exceeded", () => {
    expect(() =>
      planLargeTextAsset("x".repeat(201_000), {
        assetByteLimit: 200_000,
      }),
    ).toThrow(LargeTextAssetError);
  });
});
