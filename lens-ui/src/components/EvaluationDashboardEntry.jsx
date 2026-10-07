import EvaluationDashboard from "./EvaluationDashboard";

export default function EvaluationDashboardEntry({ onNavigate }) {
    return <div onClickCapture={(event) => {
        const button = event.target.closest("button");
        if (button?.textContent?.trim() !== "New Task") return;
        event.preventDefault();
        event.stopPropagation();
        onNavigate("optimization-evaluate-new");
    }}>
        <EvaluationDashboard onNavigate={onNavigate} />
    </div>;
}
