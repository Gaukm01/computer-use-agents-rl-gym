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
            debugs a failed job when the real problem is that the gym is down.
            Auth has three states on purpose: a cookie that exists but no longer works
            must not look healthy, and "couldn't check" is not the same as "broken". */}
        {health && (
          <>
            <span className={`pill ${health.gym_reachable ? "pill-pass" : "pill-fail"}`}>
              gym {health.gym_reachable ? "up" : "unreachable"}
            </span>
            <span
              className={`pill ${
                health.auth_valid === true
                  ? "pill-pass"
                  : health.auth_valid === false
                    ? "pill-fail"
                    : "pill-warn"
              }`}
              title={health.auth_detail ?? undefined}
            >
              auth{" "}
              {health.auth_valid === true
                ? "valid"
                : health.auth_valid === false
                  ? "invalid"
                  : "unknown"}
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
        {health && health.gym_reachable && health.auth_valid === false && (
          <div className="error-box">
            <b>Auth will not work — every attempt would report ERRORED.</b>
            <div style={{ marginTop: 4 }}>{health.auth_detail}</div>
            <div style={{ marginTop: 6 }} className="mono">
              agent/.venv/bin/python -m rlgym.capture_auth
            </div>
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
