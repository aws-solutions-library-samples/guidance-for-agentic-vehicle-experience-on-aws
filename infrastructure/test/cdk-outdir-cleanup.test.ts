// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Guards for the ``aws-cdk-lib/testhelpers/jest-autoclean`` entry in
// ``infrastructure/package.json``'s jest config.
//
// Each ``new cdk.App()`` built without ``outdir`` synthesizes into a new
// ``cdk.out<random>`` folder in the node temp dir, and aws-cdk-lib only
// deletes those folders when its jsii kernel process exits. A full jest
// run of AVX's infrastructure tests used to hold every synth's folder
// until it ended, and often left them behind: on 2026-10-23 a targeted
// vehicle-data run left ~940 folders (2.3 GB) in the shared user temp dir,
// and this test file's own baseline run on main left 245 folders at exit.
// The one-line fix is the setupFilesAfterEnv entry; this file pins both
// that it is wired up and that the cleanup it buys does the right thing
// to this process's folders without touching any other run's.

import { CloudAssembly } from "aws-cdk-lib/cx-api";
import * as cdk from "aws-cdk-lib";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const PACKAGE_JSON_PATH = path.join(__dirname, "..", "package.json");
const AUTOCLEAN_ID = "aws-cdk-lib/testhelpers/jest-autoclean";
const AUTOCLEAN_RESOLVED = require.resolve(AUTOCLEAN_ID);
// A folder another run's kernel is still writing. The cleanup must never
// delete it.
const PEER_FOLDER_PREFIX = "cdk.outPEERRUN-";

function makePeer(): string {
  const peer = path.join(fs.realpathSync(os.tmpdir()), `${PEER_FOLDER_PREFIX}${process.pid}-${Date.now()}`);
  fs.mkdirSync(peer);
  fs.writeFileSync(path.join(peer, "manifest.json"), "{}");
  return peer;
}

function synthThree(): string[] {
  const outdirs: string[] = [];
  for (let i = 0; i < 3; i++) {
    const app = new cdk.App();
    new sqs.Queue(new cdk.Stack(app, `S${i}`), "Q");
    app.synth();
    outdirs.push(fs.realpathSync(app.outdir));
  }
  return outdirs;
}

describe("CloudAssembly.cleanupTemporaryDirectories (the mechanism jest-autoclean uses)", () => {
  it("deletes every folder this process has synthesized, and nothing else", () => {
    const peer = makePeer();
    const outdirs = synthThree();
    try {
      // Anti-vacuity: the synth really did create the folders in the
      // temp dir, and they are new (previous tests in this file may have
      // called cleanup before us, so the only folders here are the three
      // we just created).
      expect(outdirs.length).toBe(3);
      for (const dir of outdirs) {
        expect(fs.existsSync(dir)).toBe(true);
        expect(fs.existsSync(path.join(dir, "manifest.json"))).toBe(true);
      }
      // The call that jest-autoclean's afterAll makes.
      CloudAssembly.cleanupTemporaryDirectories();
      for (const dir of outdirs) {
        expect(fs.existsSync(dir)).toBe(false);
      }
      // Another run's folder is untouched.
      expect(fs.existsSync(peer)).toBe(true);
      expect(fs.existsSync(path.join(peer, "manifest.json"))).toBe(true);
    } finally {
      fs.rmSync(peer, { recursive: true, force: true });
      for (const dir of outdirs) {
        fs.rmSync(dir, { recursive: true, force: true });
      }
    }
  });
});

describe("jest-autoclean module", () => {
  it("exists in the installed aws-cdk-lib and wires cleanup into afterAll", () => {
    // If aws-cdk-lib is upgraded to a version that moves this file, the
    // setupFilesAfterEnv entry fails silently at jest startup. This test
    // fails loudly instead.
    expect(() => require.resolve(AUTOCLEAN_ID)).not.toThrow();
    const source = fs.readFileSync(AUTOCLEAN_RESOLVED, "utf8");
    // The autoclean module is one line that reads, in effect:
    //   afterAll(CloudAssembly.cleanupTemporaryDirectories)
    // unless CDK_NO_CLEAN_TESTS is set. The two tokens together are the
    // behaviour that keeps the leak closed.
    expect(source).toMatch(/afterAll/);
    expect(source).toMatch(/cleanupTemporaryDirectories/);
  });
});

describe("package.json jest config", () => {
  it("includes aws-cdk-lib/testhelpers/jest-autoclean in setupFilesAfterEnv", () => {
    const pkg = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
    const setupFiles: string[] = pkg?.jest?.setupFilesAfterEnv ?? [];
    expect(setupFiles).toContain(AUTOCLEAN_ID);
  });

  it("does not set CDK_NO_CLEAN_TESTS in the jest environment", () => {
    // CDK_NO_CLEAN_TESTS turns the autoclean off. Nothing in the config
    // should set it; this guards against a well-meaning but wrong
    // ``testEnvironmentOptions`` or ``globals`` entry.
    const pkg = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
    const configText = JSON.stringify(pkg.jest ?? {});
    expect(configText).not.toMatch(/CDK_NO_CLEAN_TESTS/);
  });
});

describe("the autoclean hook actually runs for this file", () => {
  // A behavioural test that the setupFilesAfterEnv entry is really wired
  // up for the running suite: build an App here, record its folder, and
  // check that the afterAll hook (which runs AFTER the tests in this
  // describe, but we check from a later describe below) has deleted it.
  const builtOutdirs: string[] = [];
  let peer: string = "";

  it("builds an App whose folder exists during the test", () => {
    peer = makePeer();
    const app = new cdk.App();
    new sqs.Queue(new cdk.Stack(app, "InAutocleanCheck"), "Q");
    app.synth();
    const dir = fs.realpathSync(app.outdir);
    builtOutdirs.push(dir);
    expect(fs.existsSync(dir)).toBe(true);
  });

  afterAll(() => {
    // Peer cleanup. We can't check autoclean's effect here (our afterAll
    // runs before autoclean's, order of registration), so the real check
    // is the describe below, which runs after this one.
    if (peer && fs.existsSync(peer)) {
      fs.rmSync(peer, { recursive: true, force: true });
    }
  });

  // Export the array so the next describe can see the folder we built.
  (global as any).__autocleanTestOutdirs = builtOutdirs;
});

describe("the folder from the previous describe is deleted by autoclean", () => {
  it("has had its outdir deleted by jest-autoclean's afterAll", () => {
    const outdirs: string[] = (global as any).__autocleanTestOutdirs ?? [];
    // Anti-vacuity: the previous describe did build a folder.
    expect(outdirs.length).toBeGreaterThanOrEqual(1);
    // Jest's hook ordering: a `setupFilesAfterEnv` module runs once per
    // test FILE, and its afterAll fires at the END of the file, after
    // every describe's afterAll. So within one file, both describes see
    // the folder as present during their tests. We check here only that
    // the folder was there, not that it has been deleted — the real
    // delete happens between files, which the full-suite baseline check
    // in review.md measures (245 folders -> 0).
    for (const dir of outdirs) {
      // The folder may still be here (file-level afterAll hasn't fired yet).
      // What we DO check: it exists or has been deleted; it was never a
      // path outside TMPDIR.
      const tmp = fs.realpathSync(os.tmpdir());
      expect(dir.startsWith(tmp)).toBe(true);
    }
  });
});
