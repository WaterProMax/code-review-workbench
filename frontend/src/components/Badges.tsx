import type { BudgetItem } from "../api/types";
import { budgetLabel, type Token } from "../display";

export function StatusBadge({ token }: { token: Token }) {
  return (
    <span className={token.className} title={token.hint}>
      {token.label}
    </span>
  );
}

export function BudgetTable({ items }: { items: BudgetItem[] }) {
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>额度</th>
          <th>已用</th>
          <th>上限（含追加）</th>
          <th>剩余</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <tr key={item.budget_kind}>
            <td>{budgetLabel(item.budget_kind)}</td>
            <td className="mono">{item.consumed}</td>
            <td className="mono">{item.granted_max}</td>
            <td className="mono">{item.remaining}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
