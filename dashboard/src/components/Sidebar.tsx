"use client";

import { usePathname } from "next/navigation";
import Link from "next/link";
import DashboardIcon from "@mui/icons-material/Dashboard";
import SearchIcon from "@mui/icons-material/Search";
import TelegramIcon from "@mui/icons-material/Telegram";
import HistoryIcon from "@mui/icons-material/History";
import OndemandVideoIcon from "@mui/icons-material/OndemandVideo";
import GavelIcon from "@mui/icons-material/Gavel";
import NotificationsIcon from "@mui/icons-material/Notifications";
import RateReviewIcon from "@mui/icons-material/RateReview";
import ScheduleIcon from "@mui/icons-material/Schedule";
import MenuIcon from "@mui/icons-material/Menu";
import CloseIcon from "@mui/icons-material/Close";
import ShieldIcon from "@mui/icons-material/Shield";
import { IconButton } from "@mui/material";
import { useState } from "react";

const NAV_ITEMS = [
  { href: "/", label: "Dashboard", icon: DashboardIcon },
  { href: "/scan/new", label: "New Scan", icon: SearchIcon },
  { href: "/scan/telegram", label: "Telegram Scan", icon: TelegramIcon },
  { href: "/monitors", label: "Monitors", icon: ScheduleIcon },
  { href: "/alerts", label: "Alerts", icon: NotificationsIcon },
  { href: "/review", label: "Review Queue", icon: RateReviewIcon },
  { href: "/history", label: "Scan History", icon: HistoryIcon },
  { href: "/channels", label: "Flagged Channels", icon: OndemandVideoIcon },
  { href: "/reports", label: "Takedown Reports", icon: GavelIcon },
];

export default function Sidebar() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  return (
    <>
      {/* Mobile hamburger */}
      <div className="fixed left-4 top-4 z-50 lg:hidden">
        <IconButton
          onClick={() => setOpen(true)}
          className="!bg-white !shadow-md dark:!bg-gray-800"
          size="small"
        >
          <MenuIcon />
        </IconButton>
      </div>

      {/* Overlay */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/40 lg:hidden"
          onClick={() => setOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={`fixed left-0 top-0 z-50 flex h-screen w-[260px] flex-col border-r border-gray-200 bg-white transition-transform dark:border-gray-800 dark:bg-gray-900
          lg:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}
      >
        {/* Header */}
        <div className="flex items-center gap-3 border-b border-gray-200 px-5 py-5 dark:border-gray-800">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-600 text-white">
            <ShieldIcon fontSize="small" />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-sm font-bold tracking-tight">
              Trustnet Security
            </h1>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Content Detection
            </p>
          </div>
          <div className="lg:hidden">
            <IconButton size="small" onClick={() => setOpen(false)}>
              <CloseIcon fontSize="small" />
            </IconButton>
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
          {NAV_ITEMS.map(({ href, label, icon: Icon }) => {
            const active =
              href === "/"
                ? pathname === "/"
                : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                onClick={() => setOpen(false)}
                className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors
                  ${
                    active
                      ? "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                      : "text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
                  }`}
              >
                <Icon
                  fontSize="small"
                  className={active ? "text-blue-600 dark:text-blue-400" : ""}
                />
                {label}
              </Link>
            );
          })}
        </nav>

        {/* Footer */}
        <div className="border-t border-gray-200 px-5 py-4 dark:border-gray-800">
          <p className="text-xs text-gray-400">
            YouTube + Telegram Detection
          </p>
        </div>
      </aside>
    </>
  );
}
