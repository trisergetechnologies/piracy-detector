"use client";

import { useEffect, useState } from "react";
import { getHealth } from "@/lib/api";
import type { HealthResponse } from "@/lib/types";
import CircleIcon from "@mui/icons-material/Circle";

export default function HealthIndicator() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let mounted = true;
    const check = async () => {
      try {
        const data = await getHealth();
        if (mounted) {
          setHealth(data);
          setError(false);
        }
      } catch {
        if (mounted) setError(true);
      }
    };
    check();
    const id = setInterval(check, 30_000);
    return () => {
      mounted = false;
      clearInterval(id);
    };
  }, []);

  if (error) {
    return (
      <div className="flex items-center gap-2 rounded-lg bg-red-50 px-3 py-2 text-xs dark:bg-red-950">
        <CircleIcon sx={{ fontSize: 10 }} className="text-red-500" />
        <span className="font-medium text-red-700 dark:text-red-300">
          Backend Offline
        </span>
      </div>
    );
  }

  if (!health) {
    return (
      <div className="flex items-center gap-2 rounded-lg bg-gray-100 px-3 py-2 text-xs dark:bg-gray-800">
        <CircleIcon sx={{ fontSize: 10 }} className="text-gray-400" />
        <span className="text-gray-500">Checking...</span>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2 rounded-lg bg-green-50 px-3 py-2 text-xs dark:bg-green-950">
      <CircleIcon sx={{ fontSize: 10 }} className="text-green-500" />
      <span className="font-medium text-green-700 dark:text-green-300">
        Online
      </span>
      <span className="text-gray-400">|</span>
      <span className="text-gray-500 dark:text-gray-400">
        DB: {health.database}
      </span>
    </div>
  );
}
