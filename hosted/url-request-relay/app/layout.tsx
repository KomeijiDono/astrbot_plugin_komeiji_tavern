import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Komeiji Request Relay",
  description: "Short-lived request pages for model capability testing.",
  robots: {
    index: false,
    follow: false,
    noarchive: true,
  },
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
