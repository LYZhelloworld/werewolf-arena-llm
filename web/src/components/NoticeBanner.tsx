import type { ConnStatus } from "../store/gameStore";

// 顶部异常提示条：连接错误 / 断线重连时置顶显示（M8 异常提示）。
export default function NoticeBanner({
  status,
  error,
}: {
  status: ConnStatus;
  error: string | null;
}) {
  if (status === "error") {
    return (
      <div className="border-b border-rose-700 bg-rose-950/60 px-4 py-2 text-sm text-rose-200">
        <span className="font-medium">对局异常：</span>
        {error ?? "连接已断开，请确认后端仍在运行。"}
      </div>
    );
  }
  if (status === "reconnecting") {
    return (
      <div className="border-b border-amber-700 bg-amber-950/40 px-4 py-2 text-sm text-amber-200">
        连接中断，正在自动重连…
      </div>
    );
  }
  return null;
}
