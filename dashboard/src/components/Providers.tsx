"use client";

import { ThemeProvider, CssBaseline } from "@mui/material";
import { lightTheme } from "@/theme/muiTheme";
import { Toaster } from "react-hot-toast";
import { ReactNode } from "react";

export default function Providers({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider theme={lightTheme}>
      <CssBaseline />
      <Toaster
        position="top-right"
        toastOptions={{
          style: {
            borderRadius: "10px",
            background: "#1f2937",
            color: "#f9fafb",
            fontSize: "14px",
          },
        }}
      />
      {children}
    </ThemeProvider>
  );
}
