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
        <button className="btn btn-primary btn-sm" onClick={() => navigate(null)}>
          New job
        </button>
      </header>

      <main className="container">
        {/* Only ever rendered when something is genuinely broken. Setup problems make
            every attempt fail identically, so saying so here costs one line and saves
            debugging a job whose real problem was the environment. */}
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
