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
        {/* The two setup failures that make every run fail. Shown up front so nobody
            debugs a failed job when the real problem is that the gym is down. */}
        {health && (
          <>
            <span className={`pill ${health.gym_reachable ? "pill-pass" : "pill-fail"}`}>
              gym {health.gym_reachable ? "up" : "unreachable"}
            </span>
            <span className={`pill ${health.auth_captured ? "pill-pass" : "pill-fail"}`}>
              auth {health.auth_captured ? "captured" : "missing"}
            </span>
          </>
        )}
      </header>

      <main className="container">
        {health && !health.gym_reachable && (
          <div className="error-box">
            The gym is not reachable at http://localhost:3000. Start it with{" "}
            <span className="mono">colima start</span> then{" "}
            <span className="mono">cd metabase &amp;&amp; docker compose up -d</span>.
          </div>
        )}
        {health && !health.auth_captured && (
          <div className="error-box">
            No auth cookie captured, so every attempt will report ERRORED. Run{" "}
            <span className="mono">
              agent/.venv/bin/python -m rlgym.capture_auth
            </span>
            .
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
