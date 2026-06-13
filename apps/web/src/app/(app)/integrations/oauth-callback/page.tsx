"use client";

import { Loader2 } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";
import axios from "axios";

import { api, getErrorMessage } from "@/lib/api";

const POPUP_MESSAGE_TYPE = "egpt-oauth-result";

export default function OAuthCallbackPage() {
  const router = useRouter();
  const search = useSearchParams();
  const [message, setMessage] = useState("Completing OAuth handshake…");
  const handled = useRef(false);

  useEffect(() => {
    if (handled.current) return;
    handled.current = true;
    const state = search.get("state");
    const code = search.get("code");
    const error = search.get("error");

    // If we're running in a popup spawned by the inline-connect flow, send the
    // result back to the opener and close ourselves. Otherwise fall back to the
    // legacy full-page redirect behaviour (Integrations page handles it).
    const inPopup =
      typeof window !== "undefined" && Boolean(window.opener) && !window.opener.closed;

    const finishInPopup = (payload: {
      ok: boolean;
      message: string;
      state: string | null;
    }) => {
      try {
        window.opener?.postMessage({ type: POPUP_MESSAGE_TYPE, ...payload }, "*");
      } catch {
        // Cross-origin; ignore — opener can poll instead.
      }
      window.close();
    };

    if (error) {
      const msg = `OAuth was cancelled or denied: ${error}`;
      setMessage(msg);
      if (inPopup) {
        finishInPopup({ ok: false, message: msg, state });
      } else {
        toast.error(`OAuth denied: ${error}`);
      }
      return;
    }
    if (!state || !code) {
      const msg = "Missing state/code in callback URL.";
      setMessage(msg);
      if (inPopup) finishInPopup({ ok: false, message: msg, state });
      return;
    }

    (async () => {
      try {
        await api.post("/api/v1/connections/oauth/callback", { state, code });
        if (inPopup) {
          finishInPopup({ ok: true, message: "Connected.", state });
          return;
        }
        toast.success("Connected.");
      } catch (e) {
        const msg = axios.isAxiosError(e)
          ? getErrorMessage(e)
          : "OAuth callback failed.";
        if (inPopup) {
          finishInPopup({ ok: false, message: msg, state });
          return;
        }
        toast.error(msg);
      } finally {
        if (!inPopup) {
          window.sessionStorage.removeItem(`oauth_pending_${state}`);
          router.replace("/integrations");
        }
      }
    })();
  }, [router, search]);

  return (
    <div className="mx-auto max-w-md py-24 text-center">
      <Loader2 className="mx-auto h-8 w-8 animate-spin text-brand-500" />
      <p className="mt-4 text-sm text-slate-600 dark:text-slate-400">{message}</p>
    </div>
  );
}
