import { WORKFLOW_STEPS, type WorkflowStep } from "../steps";

export function StepBar({ step, waiting }: { step: WorkflowStep; waiting: boolean }) {
  return (
    <section className="panel" aria-label="任务步骤">
      <h2>当前步骤</h2>
      <ol className="step-bar">
        {WORKFLOW_STEPS.map((item) => (
          <li
            key={item}
            className={item === step ? "step-current" : undefined}
            aria-current={item === step ? "step" : undefined}
          >
            {item}
          </li>
        ))}
      </ol>
      {waiting && <p className="warn-box">等待恢复</p>}
    </section>
  );
}
