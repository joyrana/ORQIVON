import { describe, expect, it, vi } from "vitest";
import { consumeSsoCallback } from "./App";

function at(url: string) {
  window.history.replaceState(null, "", url);
}

describe("consumeSsoCallback", () => {
  it("stores the session from the fragment and removes it from the address bar", () => {
    at("/auth/callback#token=asw_abc&return_to=%2Fprojects%2Fp1%3Ftab%3D2");
    const signIn = vi.fn();
    expect(consumeSsoCallback(signIn)).toBe(true);
    expect(signIn).toHaveBeenCalledWith("asw_abc");
    expect(window.location.pathname + window.location.search).toBe("/projects/p1?tab=2");
    expect(window.location.hash).toBe("");
  });

  it("never navigates off the app", () => {
    at("/auth/callback#token=t&return_to=%2F%2Fevil.example.com");
    consumeSsoCallback(vi.fn());
    expect(window.location.pathname).toBe("/");
  });

  it("ignores other pages and callbacks without a token", () => {
    at("/projects#token=x");
    const signIn = vi.fn();
    expect(consumeSsoCallback(signIn)).toBe(false);
    at("/auth/callback");
    expect(consumeSsoCallback(signIn)).toBe(false);
    expect(signIn).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/");
  });
});
