import { AnimatePresence, motion } from "framer-motion";
import { useToasts, dismiss } from "@/store/toast";

/**
 * Write-action feedback. Success and failure both come from the API's answer;
 * there is no optimistic green flash.
 */
export function Toasts() {
  const toasts = useToasts();
  return (
    <div className="pointer-events-none fixed bottom-[74px] end-3 z-[90] flex w-[320px] flex-col gap-1.5">
      <AnimatePresence initial={false}>
        {toasts.map((toast) => (
          <motion.div
            key={toast.id}
            layout
            initial={{ opacity: 0, y: 12, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.98 }}
            transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
            role="status"
            className={`glass pointer-events-auto rounded-xl border-s-2 px-2.5 py-2 ${
              toast.tone === "error" ? "border-s-danger" : toast.tone === "ok" ? "border-s-ok" : "border-s-accent"
            }`}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className={`text-[11.5px] font-medium ${toast.tone === "error" ? "text-danger" : toast.tone === "ok" ? "text-ok" : "text-accent"}`}>
                  {toast.title}
                </p>
                {toast.detail ? <p className="mt-0.5 break-words text-[10.5px] leading-relaxed text-faint">{toast.detail}</p> : null}
              </div>
              <button
                type="button"
                onClick={() => dismiss(toast.id)}
                className="shrink-0 text-[11px] text-faint transition hover:text-ink"
                aria-label="Dismiss"
              >
                ✕
              </button>
            </div>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
