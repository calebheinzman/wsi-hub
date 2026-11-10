"use client"

import * as React from "react"
import { TooltipProvider } from "@/components/ui/tooltip"

interface AppTooltipProviderProps {
  children: React.ReactNode
}

export function AppTooltipProvider({ children }: AppTooltipProviderProps) {
  return <TooltipProvider>{children}</TooltipProvider>
}
