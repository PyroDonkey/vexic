"use client";

import { useEffect, useState } from "react";

type SupportRecord = {
  ticketId: string;
  orgId: string;
  projectIds: string[];
  status: string;
  createdAt: string;
  updatedAt: string;
};

export default function SupportView() {
  const [records, setRecords] = useState<SupportRecord[]>([]);
  const [restricted, setRestricted] = useState(false);

  useEffect(() => {
    async function load() {
      const response = await fetch("/api/control-plane/support", { cache: "no-store" });
      if (response.status === 403) {
        setRestricted(true);
        return;
      }
      if (response.ok) {
        const data = (await response.json()) as { records: SupportRecord[] };
        setRecords(data.records);
      }
    }
    void load();
  }, []);

  return (
    <>
      <header className="page-title">
        <div>
          <div className="eyebrow">Internal support</div>
          <h1>Support metadata</h1>
          <p className="muted">Ticket, organization, project, and timestamp metadata only.</p>
        </div>
      </header>
      {restricted ? (
        <section className="notice">
          <h2>Restricted</h2>
          <p>Vexic-internal support access is required.</p>
        </section>
      ) : (
        <section className="panel">
          {records.map((record) => (
            <div className="metric-row" key={record.ticketId}>
              <div>
                <strong>{record.ticketId}</strong>
                <div className="muted">{record.status}</div>
              </div>
              <div>
                <code>{record.orgId}</code>
                <div className="muted">{record.projectIds.length} projects</div>
              </div>
              <div className="muted">{record.updatedAt}</div>
            </div>
          ))}
        </section>
      )}
    </>
  );
}
