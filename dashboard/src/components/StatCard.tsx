"use client";

import { ReactNode } from "react";

interface StatCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  icon: ReactNode;
  color: "blue" | "red" | "amber" | "green" | "purple";
}

const colorMap = {
  blue: "bg-blue-50 text-blue-600 dark:bg-blue-950 dark:text-blue-400",
  red: "bg-red-50 text-red-600 dark:bg-red-950 dark:text-red-400",
  amber: "bg-amber-50 text-amber-600 dark:bg-amber-950 dark:text-amber-400",
  green: "bg-green-50 text-green-600 dark:bg-green-950 dark:text-green-400",
  purple: "bg-purple-50 text-purple-600 dark:bg-purple-950 dark:text-purple-400",
};

export default function StatCard({
  title,
  value,
  subtitle,
  icon,
  color,
}: StatCardProps) {
  return (
    <div className="card flex items-start gap-4">
      <div className={`rounded-xl p-3 ${colorMap[color]}`}>{icon}</div>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-gray-500 dark:text-gray-400">
          {title}
        </p>
        <p className="mt-1 text-2xl font-bold tracking-tight">{value}</p>
        {subtitle && (
          <p className="mt-0.5 text-xs text-gray-400 dark:text-gray-500">
            {subtitle}
          </p>
        )}
      </div>
    </div>
  );
}
