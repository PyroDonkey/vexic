import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { activeOrganizationCreateProps } from "../lib/console-routes.mjs";

const root = fileURLToPath(new URL("..", import.meta.url));
const appRoot = path.join(root, "app");

const routes = [
  ["public home", "app/page.tsx"],
  ["sign in", "app/sign-in/[[...sign-in]]/page.tsx"],
  ["project list", "app/console/page.tsx"],
  ["project workspace", "app/console/projects/[projectId]/page.tsx"],
  ["settings", "app/console/settings/page.tsx"],
  ["support", "app/console/support/page.tsx"]
];

test("required console routes exist", () => {
  for (const [name, file] of routes) {
    assert.ok(existsSync(path.join(root, file)), `${name} route missing: ${file}`);
  }
});

test("console does not expose self-serve sign-up", () => {
  const appFiles = filesUnder(appRoot);
  const signUpRouteFiles = appFiles.filter((file) => isRouteFile(file) && hasPathSegment(file, "sign-up"));
  const signUpLinks = appFiles
    .filter((file) => file.endsWith(".tsx"))
    .filter((file) => readFileSync(file, "utf8").includes("/sign-up"));

  assert.deepEqual(signUpRouteFiles.map(relativeToRoot), []);
  assert.deepEqual(signUpLinks.map(relativeToRoot), []);
});

function filesUnder(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    return entry.isDirectory() ? filesUnder(entryPath) : [entryPath];
  });
}

function isRouteFile(file) {
  return /(^|[\\/])(page|route)\.[^.]+$/.test(file);
}

function hasPathSegment(file, segment) {
  return path.relative(appRoot, file).split(path.sep).includes(segment);
}

function relativeToRoot(file) {
  return path.relative(root, file).replaceAll(path.sep, "/");
}

test("project workspace route remounts client state per project", () => {
  const source = readFileSync(path.join(root, "app/console/projects/[projectId]/page.tsx"), "utf8");

  assert.match(source, /<ProjectWorkspace key=\{projectId\} projectId=\{projectId\} \/>/);
});

test("project workspace clears one-time key reveal state when project changes", () => {
  const source = readFileSync(path.join(root, "app/console/projects/[projectId]/project-workspace.tsx"), "utf8");

  assert.match(source, /useEffect\(\(\) => \{\s+workspaceRequestSeq\.current \+= 1;\s+setRawKey\(""\);\s+setCreatedKey\(null\);\s+void loadProject\(\);/);
});

test("active organization creation redirects through a fresh server render", () => {
  assert.deepEqual(activeOrganizationCreateProps, {
    afterCreateOrganizationUrl: "/console?orgCreated=1",
    routing: "hash",
    skipInvitationScreen: true
  });
});
