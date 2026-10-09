
import type { ReactNode } from "react";
import { Link, Route, Routes, useLocation } from "react-router-dom";

import InvoiceDetailPage from "./pages/InvoiceDetailPage";
import InvoiceListPage from "./pages/InvoiceListPage";
import PoliciesPage from "./pages/PoliciesPage";
import ReviewsPage from "./pages/ReviewsPage";
import UploadPage from "./pages/UploadPage";

const navigation = [
  { label: "Home", to: "/" },
  { label: "Invoices", to: "/invoices" },
  { label: "Policies", to: "/policies" },
  { label: "Reviews", to: "/reviews" },
  { label: "Upload", to: "/upload" },
];

function isActive(pathname: string, to: string) {
  return pathname === to || (to !== "/" && pathname.startsWith(to));
}

const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#5b9bd5]/50 focus-visible:ring-offset-2";

function NavPill({
  label,
  to,
  active,
  variant,
}: {
  label: string;
  to: string;
  active: boolean;
  variant: "segment" | "mobile";
}) {
  const base =
    "shrink-0 rounded-full text-[13px] leading-none transition-all duration-200 ease-out";

  if (variant === "mobile") {
    return (
      <Link
        to={to}
        aria-current={active ? "page" : undefined}
        className={`${base} px-3.5 py-2.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#5b9bd5]/40 ${
          active
            ? "bg-[#5b9bd5] font-medium text-white shadow-[0_4px_12px_-4px_rgba(91,155,213,0.6)]"
            : "font-normal text-[#686d72] hover:bg-[#eef2f6] hover:text-[#17191b]"
        }`}
      >
        {label}
      </Link>
    );
  }

  return (
    <Link
      to={to}
      aria-current={active ? "page" : undefined}
      className={`${base} px-4 py-2.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#5b9bd5]/45 focus-visible:ring-offset-0 ${
        active
          ? "bg-white font-medium text-[#17191b] shadow-[0_1px_2px_rgba(23,25,27,0.06),0_6px_16px_-8px_rgba(23,52,74,0.35)] ring-1 ring-[#e7edf2]"
          : "font-normal text-[#686d72] hover:-translate-y-px hover:bg-white/70 hover:text-[#17191b] hover:shadow-[0_1px_2px_rgba(23,25,27,0.05)]"
      }`}
    >
      {label}
    </Link>
  );
}

function Hero() {
  return (
    <section className="relative isolate overflow-hidden rounded-[28px] bg-[#14181c] shadow-[0_2px_4px_rgba(20,24,28,0.10),0_40px_80px_-40px_rgba(11,32,50,0.65)] sm:rounded-[36px]">
      {/* layered background: blue light, ink wash, fine grid */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(120%_85%_at_88%_0%,rgba(91,155,213,0.34)_0%,rgba(91,155,213,0.10)_38%,rgba(20,24,28,0)_68%)]"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(70%_60%_at_10%_100%,rgba(71,124,174,0.28)_0%,rgba(20,24,28,0)_60%)]"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -z-10 opacity-[0.55]"
        style={{
          backgroundImage:
            "linear-gradient(to right, rgba(154,196,229,0.10) 1px, transparent 1px), linear-gradient(to bottom, rgba(154,196,229,0.10) 1px, transparent 1px)",
          backgroundSize: "64px 64px",
          WebkitMaskImage:
            "radial-gradient(115% 90% at 70% 0%, #000 0%, rgba(0,0,0,0.35) 45%, transparent 78%)",
          maskImage:
            "radial-gradient(115% 90% at 70% 0%, #000 0%, rgba(0,0,0,0.35) 45%, transparent 78%)",
        }}
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-px bg-gradient-to-r from-transparent via-[#5b9bd5]/60 to-transparent"
      />

      <div className="grid gap-12 px-6 py-12 sm:px-10 sm:py-16 lg:grid-cols-12 lg:items-center lg:gap-8 lg:px-14 lg:py-24">
        <div className="lg:col-span-7">
          <div className="flex items-center gap-3">
            <span
              aria-hidden="true"
              className="h-px w-8 bg-gradient-to-r from-transparent to-[#5b9bd5]"
            />
            <span className="inline-flex items-center gap-2 text-[10.5px] font-medium uppercase tracking-[0.22em] text-[#8fb4d1]">
              <span
                aria-hidden="true"
                className="h-1.5 w-1.5 rounded-full bg-[#5b9bd5] shadow-[0_0_0_3px_rgba(91,155,213,0.22)]"
              />
              Your finance workspace
            </span>
          </div>

          <h1 className="mt-7 font-serif text-[34px] font-normal leading-[1.04] tracking-[-0.035em] text-white sm:mt-8 sm:text-[52px] lg:text-[64px]">
            A clearer view
            <br />
            of your{" "}
            <span className="bg-gradient-to-br from-[#7fb7e6] via-[#5b9bd5] to-[#3f7cb4] bg-clip-text font-normal italic text-transparent">
              invoices.
            </span>
          </h1>

          <p className="mt-6 max-w-[48ch] text-[15px] leading-[1.75] text-[#a9b6c1] sm:mt-8 sm:text-[17px]">
            Keep invoices, policies, and reviews together in one calm, organized
            workspace.
          </p>

          <div className="mt-9 flex flex-col gap-4 sm:mt-11 sm:flex-row sm:items-center sm:gap-5">
            <Link
              to="/invoices"
              className={`group inline-flex items-center justify-center gap-2.5 rounded-full bg-[#5b9bd5] px-7 py-3.5 text-sm font-medium text-white shadow-[0_14px_30px_-12px_rgba(91,155,213,0.75)] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-[#477cae] hover:shadow-[0_18px_36px_-14px_rgba(91,155,213,0.85)] ${focusRing}`}
            >
              View invoices
              <span
                aria-hidden="true"
                className="transition-transform duration-200 ease-out group-hover:translate-x-0.5"
              >
                &rarr;
              </span>
            </Link>

            <Link
              to="/upload"
              className={`inline-flex items-center justify-center rounded-full border border-white/15 px-6 py-3.5 text-sm font-medium text-[#dfe7ee] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-[#5b9bd5]/50 hover:bg-white/[0.06] hover:text-white ${focusRing} focus-visible:ring-[#7fb7e6]/60`}
            >
              Upload invoice
            </Link>
          </div>

          <p className="mt-7 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[12.5px] text-[#7d8b97] sm:mt-9">
            <span>Invoices</span>
            <span aria-hidden="true" className="h-1 w-1 rounded-full bg-[#4a5a67]" />
            <span>Policies</span>
            <span aria-hidden="true" className="h-1 w-1 rounded-full bg-[#4a5a67]" />
            <span>Reviews</span>
            <span aria-hidden="true" className="h-1 w-1 rounded-full bg-[#4a5a67]" />
            <span>Everything in its place</span>
          </p>
        </div>

        {/* abstract document composition */}
        <div className="relative hidden lg:col-span-5 lg:block">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute -inset-10 -z-10 rounded-full bg-[#5b9bd5]/20 blur-3xl"
          />

          <div
            aria-hidden="true"
            className="relative ml-auto w-full max-w-[320px] rotate-[3deg] rounded-2xl border border-white/10 bg-white/[0.04] p-5 shadow-[0_30px_60px_-30px_rgba(0,0,0,0.8)] backdrop-blur-sm"
          >
            <div className="flex items-center justify-between">
              <span className="h-2 w-16 rounded-full bg-white/25" />
              <span className="h-2 w-8 rounded-full bg-[#5b9bd5]/70" />
            </div>
            <div className="mt-6 space-y-2.5">
              <span className="block h-1.5 w-full rounded-full bg-white/10" />
              <span className="block h-1.5 w-11/12 rounded-full bg-white/10" />
              <span className="block h-1.5 w-8/12 rounded-full bg-white/10" />
            </div>
            <div className="mt-6 h-24 rounded-lg border border-white/10 bg-white/[0.03] p-3">
              <div className="flex h-full items-end gap-1.5">
                <span className="h-1/3 flex-1 rounded-sm bg-white/10" />
                <span className="h-2/3 flex-1 rounded-sm bg-white/15" />
                <span className="h-1/2 flex-1 rounded-sm bg-[#5b9bd5]/40" />
                <span className="h-full flex-1 rounded-sm bg-[#5b9bd5]/70" />
              </div>
            </div>
            <div className="mt-5 flex items-center justify-between border-t border-white/10 pt-4">
              <span className="h-1.5 w-20 rounded-full bg-white/15" />
              <span className="h-1.5 w-10 rounded-full bg-white/30" />
            </div>
          </div>

          <div
            aria-hidden="true"
            className="absolute -left-2 bottom-10 w-[210px] -rotate-[6deg] rounded-2xl border border-[#5b9bd5]/25 bg-[#1b232b] p-4 shadow-[0_24px_50px_-28px_rgba(0,0,0,0.9)]"
          >
            <span className="block h-1.5 w-14 rounded-full bg-[#5b9bd5]/60" />
            <div className="mt-4 space-y-2">
              <span className="block h-1.5 w-full rounded-full bg-white/10" />
              <span className="block h-1.5 w-7/12 rounded-full bg-white/10" />
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function SectionMarkers() {
  const markers = [
    {
      to: "/invoices",
      label: "Invoices",
      note: "Every document, one ledger",
    },
    {
      to: "/policies",
      label: "Policies",
      note: "Terms kept beside the work",
    },
    {
      to: "/reviews",
      label: "Reviews",
      note: "Approvals in the same view",
    },
  ];

  return (
    <nav
      aria-label="Workspace sections"
      className="mt-6 grid gap-3 sm:mt-8 sm:grid-cols-3 sm:gap-4"
    >
      {markers.map(({ to, label, note }) => (
        <Link
          key={to}
          to={to}
          className={`group flex items-center justify-between gap-4 rounded-2xl border border-[#e7edf2] bg-white/70 px-5 py-4 transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-[#dcebfb] hover:bg-white hover:shadow-[0_10px_30px_-18px_rgba(23,52,74,0.45)] ${focusRing}`}
        >
          <span className="flex min-w-0 flex-col">
            <span className="text-[13.5px] font-medium text-[#17191b]">
              {label}
            </span>
            <span className="mt-0.5 truncate text-[12.5px] text-[#858b90]">
              {note}
            </span>
          </span>
          <span
            aria-hidden="true"
            className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-[#eef7fd] text-[13px] text-[#477cae] transition-transform duration-200 ease-out group-hover:translate-x-0.5"
          >
            &rarr;
          </span>
        </Link>
      ))}
    </nav>
  );
}

function Layout({ children }: { children: ReactNode }) {
  const location = useLocation();

  return (
    <div className="flex min-h-screen flex-col bg-[#fcfdfe] font-sans text-[#17191b] antialiased">
      <a
        href="#main-content"
        className={`sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-white focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:shadow-[0_8px_30px_rgba(23,25,27,0.12)] ${focusRing}`}
      >
        Skip to content
      </a>

      <header className="sticky top-0 z-30 px-3 pt-3 sm:px-5 sm:pt-4">
        <div className="mx-auto max-w-6xl rounded-[22px] border border-[#e7edf2]/90 bg-[#fcfdfe]/85 shadow-[0_1px_2px_rgba(23,25,27,0.04),0_18px_40px_-28px_rgba(23,52,74,0.45)] backdrop-blur-xl">
          <div className="flex h-16 items-center justify-between gap-3 px-3 sm:px-4">
            <Link
              to="/"
              aria-label="Invoice Intelligence — home"
              className={`group flex shrink-0 items-center gap-3 rounded-xl pr-2 transition-opacity duration-200 hover:opacity-90 ${focusRing}`}
            >
              <span className="relative grid h-9 w-9 place-items-center overflow-hidden rounded-[13px] bg-gradient-to-br from-[#6ba9dd] via-[#5b9bd5] to-[#3f7cb4] shadow-[0_6px_14px_-6px_rgba(71,124,174,0.7)] ring-1 ring-inset ring-white/25">
                <span
                  aria-hidden="true"
                  className="absolute inset-x-0 top-0 h-px bg-white/40"
                />
                <span className="font-serif text-[17px] leading-none text-white">
                  I
                </span>
              </span>
              <span className="flex flex-col leading-none">
                <span className="font-serif text-[16px] tracking-[-0.015em] text-[#17191b] sm:text-[17px]">
                  Invoice Intelligence
                </span>
                <span className="mt-1 hidden text-[9.5px] font-medium uppercase tracking-[0.2em] text-[#9aa3aa] sm:block">
                  Ledger &middot; Policies &middot; Reviews
                </span>
              </span>
            </Link>

            <nav
              className="hidden items-center gap-1 rounded-full border border-[#eaeff4] bg-[#f1f4f7] p-1 md:flex"
              aria-label="Main navigation"
            >
              {navigation.map(({ label, to }) => (
                <NavPill
                  key={to}
                  label={label}
                  to={to}
                  active={isActive(location.pathname, to)}
                  variant="segment"
                />
              ))}
            </nav>

            <Link
              to="/upload"
              className={`group flex shrink-0 items-center gap-2 rounded-full bg-[#17191b] px-4 py-2.5 text-[13px] font-medium leading-none text-white transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-[#24292e] hover:shadow-[0_10px_22px_-12px_rgba(23,25,27,0.8)] ${focusRing}`}
            >
              <span
                aria-hidden="true"
                className="text-[15px] leading-none text-[#7fb7e6] transition-transform duration-200 group-hover:rotate-90"
              >
                +
              </span>
              Add invoice
            </Link>
          </div>

          <nav
            className="no-scrollbar flex items-center gap-1.5 overflow-x-auto px-3 pb-3 pt-1 md:hidden"
            aria-label="Mobile navigation"
          >
            {navigation.map(({ label, to }) => (
              <NavPill
                key={to}
                label={label}
                to={to}
                active={isActive(location.pathname, to)}
                variant="mobile"
              />
            ))}
          </nav>
        </div>
      </header>

      <main
        id="main-content"
        className="mx-auto w-full max-w-6xl flex-1 px-5 pb-16 pt-7 sm:px-8 sm:pb-20 sm:pt-10"
      >
        <Hero />
        <SectionMarkers />

        <div className="min-h-[40vh]">{children}</div>

        <footer className="mt-20 border-t border-[#e7edf2] pt-10 sm:mt-28 sm:pt-14">
          <div className="flex flex-col gap-8 sm:flex-row sm:items-start sm:justify-between">
            <div className="max-w-xs">
              <p className="font-serif text-[19px] leading-tight tracking-[-0.02em] text-[#17191b]">
                Invoice Intelligence
              </p>
              <p className="mt-2 text-[13px] leading-6 text-[#858b90]">
                Everything in its place.
              </p>
            </div>

            <nav
              aria-label="Footer navigation"
              className="flex flex-wrap gap-x-6 gap-y-2"
            >
              {navigation.map(({ label, to }) => (
                <Link
                  key={to}
                  to={to}
                  className={`text-[13px] text-[#858b90] transition-colors duration-200 hover:text-[#477cae] ${focusRing}`}
                >
                  {label}
                </Link>
              ))}
            </nav>
          </div>
        </footer>
      </main>
    </div>
  );
}

function NotFoundPage() {
  return (
    <section className="mt-10 overflow-hidden rounded-[28px] border border-[#e7edf2] bg-white p-8 shadow-[0_1px_2px_rgba(23,25,27,0.03),0_24px_60px_-40px_rgba(23,52,74,0.5)] sm:p-12">
      <p className="text-[10.5px] font-semibold uppercase tracking-[0.22em] text-[#5c7f9b]">
        404 &middot; Page not found
      </p>

      <h1 className="mt-5 font-serif text-[30px] leading-[1.12] tracking-[-0.03em] text-[#17191b] sm:text-[40px]">
        We couldn&rsquo;t find that page.
      </h1>

      <p className="mt-4 max-w-md text-[15px] leading-7 text-[#71777c]">
        The page you requested does not exist.
      </p>

      <Link
        to="/"
        className={`mt-8 inline-flex items-center justify-center rounded-full bg-[#5b9bd5] px-5 py-3 text-sm font-medium text-white shadow-[0_12px_26px_-14px_rgba(91,155,213,0.8)] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-[#477cae] ${focusRing}`}
      >
        Back to invoices
      </Link>
    </section>
  );
}

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<InvoiceListPage />} />
        <Route path="/invoices" element={<InvoiceListPage />} />
        <Route path="/invoices/:id" element={<InvoiceDetailPage />} />
        <Route path="/policies" element={<PoliciesPage />} />
        <Route path="/reviews" element={<ReviewsPage />} />
        <Route path="/upload" element={<UploadPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </Layout>
  );
}
