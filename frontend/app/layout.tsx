import type { Metadata } from "next"
import "./globals.css"
import { AppTooltipProvider } from "@/components/app-tooltip-provider"

export const metadata: Metadata = {
  title: "WSI Hub",
  description: "Interactive tool for exploring whole-slide imaging datasets and research papers",
  icons: {
    icon: "/lucide-microscope.svg",
  },
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-gray-50">
        <AppTooltipProvider>{children}</AppTooltipProvider>
      </body>
    </html>
  )
}
