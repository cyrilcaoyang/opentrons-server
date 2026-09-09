// Static rendering only: never perform gateway requests or robot actions.
Object.assign(globalThis, {
  window: { location: { pathname: "/ui/", origin: "http://localhost" } },
  fetch: () => { throw new Error("Network access is forbidden in preview tests"); },
});
