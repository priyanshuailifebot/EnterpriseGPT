/**
 * Open an OAuth authorize URL in a centred popup window and resolve when the
 * callback page posts a message back. Used by the inline "Connect" buttons on
 * the canvas so the user doesn't leave the workflow editor.
 *
 * The callback page (`/integrations/oauth-callback`) detects `window.opener`
 * and posts a message of type `egpt-oauth-result` before closing itself.
 */

import { api, getErrorMessage } from "@/lib/api";
import axios from "axios";

const POPUP_MESSAGE_TYPE = "egpt-oauth-result";
const POPUP_WIDTH = 520;
const POPUP_HEIGHT = 640;

export type OAuthPopupResult = {
  ok: boolean;
  message: string;
};

export async function startInlineOAuth({
  provider,
  workspaceId,
  connectionName,
}: {
  provider: string;
  workspaceId: string;
  connectionName: string;
}): Promise<OAuthPopupResult> {
  let redirectUrl: string;
  try {
    const { data } = await api.post<{ redirect_url: string; state: string }>(
      `/api/v1/connections/oauth/${provider}/authorize?workspace_id=${workspaceId}&connection_name=${encodeURIComponent(connectionName)}`,
    );
    redirectUrl = data.redirect_url;
  } catch (e) {
    return {
      ok: false,
      message: axios.isAxiosError(e) ? getErrorMessage(e) : "Could not start OAuth.",
    };
  }

  const left = window.screenX + (window.outerWidth - POPUP_WIDTH) / 2;
  const top = window.screenY + (window.outerHeight - POPUP_HEIGHT) / 2;
  const popup = window.open(
    redirectUrl,
    "egpt-oauth",
    `width=${POPUP_WIDTH},height=${POPUP_HEIGHT},left=${left},top=${top}`,
  );
  if (!popup) {
    return {
      ok: false,
      message: "Popup blocked — allow popups for this site and try again.",
    };
  }

  return await new Promise<OAuthPopupResult>((resolve) => {
    let settled = false;
    const settle = (r: OAuthPopupResult) => {
      if (settled) return;
      settled = true;
      window.removeEventListener("message", onMessage);
      clearInterval(closedPoll);
      resolve(r);
    };

    const onMessage = (evt: MessageEvent) => {
      const data = evt.data as { type?: string; ok?: boolean; message?: string };
      if (!data || data.type !== POPUP_MESSAGE_TYPE) return;
      settle({ ok: !!data.ok, message: data.message ?? "" });
    };
    window.addEventListener("message", onMessage);

    // If the user closes the popup without finishing, treat it as a cancel.
    const closedPoll = window.setInterval(() => {
      if (popup.closed) {
        settle({ ok: false, message: "OAuth window was closed." });
      }
    }, 600);
  });
}
