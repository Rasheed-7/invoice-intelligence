import { useRef, useState, type ChangeEvent, type DragEvent } from "react";

/** Extensions offered by the native picker; the page re-validates the file. */
const ACCEPT_ATTRIBUTE = ".pdf,.png,.jpg,.jpeg";

const FOCUS_RING =
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-[#5b9bd5]/55 focus-visible:ring-offset-2";

interface FileDropZoneProps {
  /** Invoked with the first dropped or selected file. */
  onFileSelected: (file: File) => void;
}

/** Small stacked-document mark; CSS only, no image dependency. */
function UploadMark() {
  return (
    <span
      aria-hidden="true"
      className="relative grid h-14 w-14 place-items-center rounded-2xl bg-[#eef7fd] ring-1 ring-inset ring-[#dcebfb]"
    >
      <span className="absolute inset-x-0 top-0 h-px rounded-t-2xl bg-white/70" />
      <svg viewBox="0 0 24 24" fill="none" className="h-6 w-6 text-[#477cae]">
        <path
          d="M12 15.5V4.5m0 0L8.25 8.25M12 4.5l3.75 3.75"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path
          d="M4.75 15v2.75a1.5 1.5 0 0 0 1.5 1.5h11.5a1.5 1.5 0 0 0 1.5-1.5V15"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
        />
      </svg>
    </span>
  );
}

/**
 * Presentational drag-and-drop target with a hidden native file picker.
 *
 * It owns only the transient "dragging" highlight; validation and uploading are
 * the caller's responsibility.
 */
export default function FileDropZone({ onFileSelected }: FileDropZoneProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isDragging, setIsDragging] = useState(false);

  function openPicker(): void {
    fileInputRef.current?.click();
  }

  function handleDragOver(event: DragEvent<HTMLDivElement>): void {
    // Required, otherwise the browser refuses the drop and opens the file instead.
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setIsDragging(true);
  }

  function handleDragLeave(event: DragEvent<HTMLDivElement>): void {
    // dragleave also fires when crossing child nodes; ignore those transitions.
    const next = event.relatedTarget;
    if (next instanceof Node && event.currentTarget.contains(next)) {
      return;
    }
    setIsDragging(false);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>): void {
    event.preventDefault();
    setIsDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) onFileSelected(file);
  }

  function handleInputChange(event: ChangeEvent<HTMLInputElement>): void {
    const file = event.target.files?.[0];
    // Clear the input so re-selecting the same file still fires `change`.
    event.target.value = "";
    if (file) onFileSelected(file);
  }

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label="Upload an invoice: drop a file here or press Enter to choose a file"
      onDragEnter={handleDragOver}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      onClick={openPicker}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openPicker();
        }
      }}
      className={`flex cursor-pointer flex-col items-center justify-center gap-0 rounded-[22px] border-2 border-dashed px-6 py-16 text-center transition-all duration-200 ease-out motion-reduce:transition-colors sm:px-10 sm:py-20 ${FOCUS_RING} ${
        isDragging
          ? "border-[#5b9bd5] bg-[#f4fafe] shadow-[0_0_0_4px_rgba(91,155,213,0.12)]"
          : "border-[#dfe7ee] bg-[#fbfcfd] hover:border-[#cbdcea] hover:bg-[#f9fbfd]"
      }`}
    >
      <UploadMark />

      <p className="mt-7 font-serif text-[24px] leading-tight tracking-[-0.025em] text-[#17191b] sm:text-[28px]">
        {isDragging ? "Release to upload" : "Drop your invoice here"}
      </p>

      <p className="mt-3 max-w-sm text-[14px] leading-6 text-[#71777c]">
        We&rsquo;ll extract the vendor, invoice number, dates and line items
        automatically.
      </p>

      <p className="mt-4 text-[12.5px] text-[#9aa3aa]">
        PDF, PNG or JPEG, up to 20 MB
      </p>

      {/*
        The button is the keyboard focus target for the whole zone; the click
        handler stops propagation so the zone's own handler cannot fire the
        picker a second time.
      */}
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          openPicker();
        }}
        className={`mt-8 inline-flex items-center justify-center gap-2 rounded-full bg-[#5b9bd5] px-6 py-3 text-sm font-medium text-white shadow-[0_12px_26px_-14px_rgba(91,155,213,0.85)] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-[#477cae] hover:shadow-[0_16px_32px_-16px_rgba(91,155,213,0.95)] motion-reduce:transform-none motion-reduce:transition-colors ${FOCUS_RING}`}
      >
        Choose a file
      </button>

      <input
        ref={fileInputRef}
        type="file"
        accept={ACCEPT_ATTRIBUTE}
        className="hidden"
        onChange={handleInputChange}
      />
    </div>
  );
}