import { createHash, createHmac } from "node:crypto";
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";
import { z } from "zod";

import { buildApp } from "../../src/app.js";
import { linkedJourneyDetailSchema } from "../../src/modules/journey-linking/output.js";
import { createJourneyLinkingService } from "../../src/modules/journey-linking/service.js";
import { telemetrySpanRecordSchema } from "../../src/modules/journey-ingest/otlp-schema.js";
import { linkHere } from "../../src/modules/journey-lab/linking.js";
import type { ApiConfig } from "../../src/plugins/config.js";
import type { AuthenticatedPrincipal } from "../../src/ports/external-auth-access-token.js";
import { insertActiveOrganizationGraph } from "../support/auth-tenancy-builders.js";
import { withApiDatabaseTestFixture } from "../support/database.js";
import { createExternalAuthStub } from "../support/external-auth.js";

const path = process.env.AGENTRAIL_PYTHON_CAPTURE;
if (path === undefined)
  throw new Error("AGENTRAIL_PYTHON_CAPTURE is required; run integration/run.sh.");
const capture = z
  .object({
    provenance: z.string(),
    exports: z.array(
      z.object({ signal: z.enum(["traces", "logs"]), protobuf: z.string() }),
    ),
    traceId: z.string(),
    rootSpanId: z.string(),
    milestoneSpanId: z.string(),
    supportingSpanId: z.string(),
    expected: z.object({
      entries: z.number(),
      spans: z.number(),
      logs: z.number(),
    }),
  })
  .parse(JSON.parse(readFileSync(path, "utf8")));
const NOW = new Date();
const config: ApiConfig = {
  appEnv: "test",
  host: "127.0.0.1",
  port: 0,
  corsAllowedOrigins: [],
  database: {
    connectionString: "postgresql://unused",
    maxConnections: 1,
    connectionTimeoutMs: 1000,
    idleTimeoutMs: 1000,
  },
};
const hash = (id: string) =>
  createHmac("sha256", "synthetic-python-receiver-hash-key")
    .update(id)
    .digest("hex")
    .slice(0, 32);
const eventId = (spanId: string) => `otel:${capture.traceId}:${spanId}`;

// Fails if the Python SDK stops emitting its component identity, entry/milestone
// contract, hashed principal, supporting ancestry or exact log correlation.
// Replaying unchanged protobuf deliveries also guards receiver deduplication.
describe("public Python SDK → real Argus protobuf receiver → persisted journeys", () => {
  it("preserves Python entries, causal subtrees, identity and logs across reversed retries", async () => {
    await withApiDatabaseTestFixture(async ({ runtime }) => {
      const graph = await insertActiveOrganizationGraph(runtime.db, {
        userExternalAuthId: "synthetic-python-owner",
        orgExternalAuthId: "synthetic-python-org",
      });
      const scope = {
        orgId: graph.organization.id,
        workspaceId: graph.workspace.id,
      };
      const principal: AuthenticatedPrincipal = {
        mode: "direct_user",
        caller: {
          type: "user",
          externalAuthUserId: graph.user.external_auth_id,
        },
        authority: { externalAuthUserId: graph.user.external_auth_id },
        externalAuthOrgId: graph.organization.external_auth_id!,
        externalAuthSessionId: "synthetic-python-session",
        tokenId: "synthetic-python-token",
        issuedAt: new Date(NOW.getTime() - 60000),
        expiresAt: new Date(NOW.getTime() + 3600000),
      };
      const app = await buildApp({
        config,
        database: { ...runtime, close: () => Promise.resolve() },
        authTenant: {
          accessTokenVerifier: {
            verifyAccessToken: () => Promise.resolve(principal),
          },
          externalAuth: createExternalAuthStub(),
          now: () => NOW,
        },
      });
      try {
        const created = await app.inject({
          method: "POST",
          url: `/v1/workspaces/${scope.workspaceId}/journey-sources`,
          headers: { authorization: "Bearer synthetic-python-owner" },
          payload: { kind: "node", label: "Synthetic Python receiver gate" },
        });
        // Never include this response in assertion messages: it contains a source secret.
        expect(created.statusCode).toBe(201);
        const source = z.object({ secret: z.string() }).parse(created.json());
        for (let delivery = 0; delivery < 2; delivery++) {
          // Logs first, then reverse span completion batches. Retries use the exact bytes.
          const packets = [...capture.exports]
            .reverse()
            .sort(
              (a, b) =>
                Number(b.signal === "logs") - Number(a.signal === "logs"),
            );
          for (const packet of packets) {
            const receipt = await app.inject({
              method: "POST",
              url: `/ingest/otlp/v1/${packet.signal}`,
              headers: {
                authorization: `Bearer ${source.secret}`,
                "content-type": "application/x-protobuf",
              },
              payload: Buffer.from(packet.protobuf, "base64"),
            });
            expect(
              receipt.statusCode,
              `Python ${packet.signal} delivery ${delivery + 1}`,
            ).toBe(200);
          }
        }
        // Fresh reads prove immutable committed records survived identical retry deliveries.
        const storedSpans = await runtime.db
          .selectFrom("journey_telemetry_spans")
          .select(["record"])
          .where("org_id", "=", scope.orgId)
          .where("workspace_id", "=", scope.workspaceId)
          .execute();
        const spans = storedSpans.map((row) =>
          telemetrySpanRecordSchema.parse(row.record),
        );
        const storedLogs = await runtime.db
          .selectFrom("journey_telemetry_logs")
          .select(["record_id", "trace_id", "span_id", "record"])
          .where("org_id", "=", scope.orgId)
          .where("workspace_id", "=", scope.workspaceId)
          .execute();
        expect(spans).toHaveLength(capture.expected.spans);
        expect(storedLogs).toHaveLength(capture.expected.logs);
        expect(storedLogs.map((log) => z.object({eventName:z.string()}).parse(log.record).eventName).sort()).toEqual(["synthetic.milestone", "synthetic.supporting"]);
        expect(
          new Set(spans.map((span) => `${span.traceId}:${span.spanId}`)).size,
        ).toBe(capture.expected.spans);
        expect(new Set(storedLogs.map((log) => log.record_id)).size).toBe(
          capture.expected.logs,
        );
        expect(
          spans.every((span) => span.componentName === "agentrail-python"),
        ).toBe(true);
        expect(
          spans.every(
            (span) =>
              span.hashKeyId ===
              createHash("sha256")
                .update("synthetic-python-receiver-hash-key")
                .digest("hex")
                .slice(0, 16),
          ),
        ).toBe(true);
        expect(
          spans.find((span) => span.spanId === capture.milestoneSpanId)
            ?.parentSpanId,
        ).toBe(capture.rootSpanId);
        expect(
          spans.find((span) => span.spanId === capture.supportingSpanId)
            ?.parentSpanId,
        ).toBe(capture.milestoneSpanId);
        const entrySpans = spans.filter((span) => span.entryEventId !== null);
        expect(
          entrySpans.map((span) => span.entryEventId).sort(),
          "Python server entry and explicit milestone must both be admitted",
        ).toEqual(
          [
            eventId(capture.rootSpanId),
            eventId(capture.milestoneSpanId),
          ].sort(),
        );

        const result = await createJourneyLinkingService({
          reportFailure: (error) => {
            throw error;
          },
          database: runtime.db,
          now: () => NOW,
          linker: (request) => Promise.resolve(linkHere(request)),
        }).link(scope);
        expect(result.kind).toBe("linked");
        const stored = await runtime.db
          .selectFrom("linked_journeys")
          .select(["detail"])
          .where("org_id", "=", scope.orgId)
          .where("workspace_id", "=", scope.workspaceId)
          .execute();
        const details = stored.map((row) =>
          linkedJourneyDetailSchema.parse(row.detail),
        );
        expect(details).toHaveLength(1);
        const events = details.flatMap(
          (detail) => detail.telemetry?.events ?? [],
        );
        expect(events).toHaveLength(capture.expected.entries);
        for (const event of events) {
          expect(event.emittedBy).toBe("argus");
          expect(event.actor.location).toEqual({
            country: "US", region: "Washington", city: "Seattle", source: "source",
            observedAt: new Date(event.start).toISOString(),
          });
          expect(event.principal).toEqual({
            kind: "user",
            id: hash("synthetic-python-user"),
            actingUserId: hash("synthetic-python-acting-user"),
          });
        }
        const milestone = events.find(
          (event) => event.id === eventId(capture.milestoneSpanId),
        );
        expect(milestone?.surface).toBe("custom");
        expect(milestone?.kind).toBe("checkout.confirm");
        expect(milestone?.span?.subtreeSpanIds).toContain(
          capture.supportingSpanId,
        );
        const supportingIds = spans
          .filter((span) => span.entryEventId === null)
          .map((span) => span.spanId)
          .sort();
        expect(milestone?.span?.subtreeSpanIds).toEqual(supportingIds);
        expect(
          details
            .flatMap((detail) => detail.links)
            .some((link) => link.rule === "trace" && link.status === "active"),
        ).toBe(true);
        const summaries = details.flatMap(
          (detail) => detail.telemetry?.logs ?? [],
        );
        expect(summaries.reduce((sum, summary) => sum + summary.logs, 0)).toBe(
          capture.expected.logs,
        );
        expect(
          summaries
            .map((summary) => ({
              eventId: summary.eventId,
              traceId: summary.traceId,
              spanId: summary.spanId,
            }))
            .sort((a, b) => String(a.spanId).localeCompare(String(b.spanId))),
        ).toEqual(
          [
            {
              eventId: eventId(capture.milestoneSpanId),
              traceId: capture.traceId,
              spanId: capture.milestoneSpanId,
            },
            {
              eventId: eventId(capture.milestoneSpanId),
              traceId: capture.traceId,
              spanId: capture.supportingSpanId,
            },
          ].sort((a, b) => a.spanId.localeCompare(b.spanId)),
        );
        expect(
          summaries.reduce((sum, summary) => sum + summary.errors, 0),
        ).toBe(1);
        expect(
          details.flatMap((detail) => detail.telemetry?.diagnostics ?? []),
        ).toEqual([]);
      } finally {
        await app.close();
      }
    });
  });
});
