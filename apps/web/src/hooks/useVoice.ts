"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type VoiceStatus = "idle" | "listening";

export interface UseVoiceHandlers {
  onTranscript(text: string): void;
}

export function useVoice(handlers: UseVoiceHandlers) {
  const recognitionRef = useRef<any>(null);
  const synthRef =
    typeof window !== "undefined" ? window.speechSynthesis : null;
  const [status, setStatus] = useState<VoiceStatus>("idle");

  useEffect(() => {
    return () => {
      const r = recognitionRef.current;
      if (r?.stop) {
        try {
          r.stop();
        } catch {
          /* ignore */
        }
      }
    };
  }, []);

  const startListening = useCallback(() => {
    if (typeof window === "undefined") return;
    const AnyWin = window as unknown as Record<string, any>;
    const SR = AnyWin.SpeechRecognition ?? AnyWin.webkitSpeechRecognition;
    if (!SR) return;
    const recognition = new SR();
    recognition.lang = "en-US";
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.onresult = (event: Record<string, any>) => {
      const transcript = [...(event.results as unknown as Array<any[]>)]
        .map((group) => String(group?.[0]?.transcript ?? ""))
        .filter(Boolean)
        .join(" ")
        .trim();
      handlers.onTranscript(transcript);
      setStatus("idle");
    };
    recognition.onend = () => setStatus("idle");
    recognition.onerror = () => setStatus("idle");
    recognition.start();
    recognitionRef.current = recognition;
    setStatus("listening");
  }, [handlers]);

  const stopListening = useCallback(() => {
    const r = recognitionRef.current;
    if (r?.stop) {
      try {
        r.stop();
      } catch {
        /* ignore */
      }
    }
    recognitionRef.current = null;
    setStatus("idle");
  }, []);

  const speak = useCallback(
    (text: string, lang = "en-US") => {
      if (!synthRef || !text) return;
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = lang;
      synthRef.cancel();
      synthRef.speak(utterance);
    },
    [synthRef],
  );

  return {
    startListening,
    stopListening,
    speak,
    isListening: status === "listening",
  };
}
