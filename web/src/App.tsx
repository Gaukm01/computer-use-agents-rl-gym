import { useEffect, useState } from "react";
import { api } from "./api";
import { JobView } from "./JobView";
import { NewRun } from "./NewRun";
import type { Health } from "./types";

/** Hash routing, hand-rolled. Two routes don't justify a router dependency, and this
 *  keeps job URLs shareable. */
function useHashRoute(): [string | null, (jobId: string | null) => void] {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  const match = hash.match(/^#\/jobs\/(.+)$/);
  const navigate = (jobId: string | null) => {
    window.location.hash = jobId ? `/jobs/${jobId}` : "";
  };
  return [match ? match[1] : null, navigate];
}

export function App() {
  const [jobId, navigate] = useHashRoute();
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  return (
    <>
      <header className="app-header">
        <div className="app-title">
          Metabase Computer-Use RL Gym <span>· eval console</span>
        </div>
        <div className="spacer" />
        {/* There is no long-lived gym to report on -- each attempt provisions its own.
            These are the things whose absence would make every attempt fail, shown up
            front so nobody debugs a failed job when the real problem is setup. */}
        {health && (
          <>
            <span
              className={`pill ${health.docker_available ? "pill-pass" : "pill-fail"}`}
            >
              docker {health.docker_available ? "ok" : "down"}
            </span>
            <span
              className={`pill ${health.images_present ? "pill-pass" : "pill-fail"}`}
              title="Images must be pulled ahead of time — a cold pull inside an attempt reads as a hang"
            >
              images {health.images_present ? "ready" : "missing"}
            </span>
            <span
              className={`pill ${health.credentials_configured ? "pill-pass" : "pill-fail"}`}
            >
              creds {health.credentials_configured ? "set" : "missing"}
            </span>
            <span className="pill pill-muted" title="Concurrent gyms, bounded by memory">
              max {health.max_gyms} gyms
            </span>
          </>
        )}
      </header>

      <main className="container">
        {health?.detail && (
          <div className="error-box">
            <b>Attempts will fail until this is fixed.</b>
            <div style={{ marginTop: 4 }}>{health.detail}</div>
          </div>
        )}

        {jobId ? (
          <JobView jobId={jobId} onBack={() => navigate(null)} />
        ) : (
          <NewRun onOpenJob={(id) => navigate(id)} />
        )}
      </main>
    </>
  );
}
