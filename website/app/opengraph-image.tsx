import { ImageResponse } from "next/og";

export const alt = "Vexic · Memory your agents can trust";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpengraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          padding: "72px",
          background:
            "radial-gradient(1000px 600px at 78% -10%, rgba(16,185,129,0.22), rgba(16,185,129,0) 60%), #0A0E0C",
          color: "#E7ECEA",
          fontFamily: "sans-serif"
        }}
      >
        <div style={{ display: "flex", alignItems: "center" }}>
          <div
            style={{
              width: 34,
              height: 34,
              borderRadius: 8,
              background: "#34D399",
              marginRight: 18
            }}
          />
          <div
            style={{
              fontSize: 30,
              fontWeight: 700,
              letterSpacing: "0.18em"
            }}
          >
            VEXIC
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column" }}>
          <div
            style={{
              fontSize: 82,
              fontWeight: 700,
              lineHeight: 1.05,
              letterSpacing: "-0.02em"
            }}
          >
            Memory your agents
          </div>
          <div
            style={{
              fontSize: 82,
              fontWeight: 700,
              lineHeight: 1.05,
              letterSpacing: "-0.02em",
              color: "#34D399"
            }}
          >
            can trust.
          </div>
        </div>

        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-end"
          }}
        >
          <div
            style={{
              fontSize: 28,
              color: "#9AA6A1",
              maxWidth: 720,
              lineHeight: 1.3
            }}
          >
            Local-first, provenance-first memory for long-running AI agents.
          </div>
          <div style={{ fontSize: 28, fontWeight: 600, color: "#34D399" }}>
            vexic.dev
          </div>
        </div>
      </div>
    ),
    { ...size }
  );
}
