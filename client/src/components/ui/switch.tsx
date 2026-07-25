import * as React from "react"
import { Switch as SwitchPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

function Switch({
  className,
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      className={cn(
        // Track. INK when on (the v4 design reserves seal/coral for the brand
        // mark, "running" state, and urgent to-dos — a plain on/off toggle is
        // none of those); line-strong gray when off. 34×20 with a 16px thumb
        // leaves exactly 2px padding on both sides in either state.
        "sj-motion-toggle peer inline-flex h-[20px] w-[34px] shrink-0 cursor-pointer items-center rounded-full border-0 p-0 outline-none",
        "focus-visible:ring-2 focus-visible:ring-[var(--sj-ink)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-surface)]",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "data-[state=checked]:bg-[var(--sj-ink)] data-[state=unchecked]:bg-[var(--sj-line-strong)]",
        className
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        data-slot="switch-thumb"
        className={cn(
          "sj-motion-toggle-thumb pointer-events-none block h-[16px] w-[16px] rounded-full bg-white shadow-[var(--sj-shadow-1)] ring-0",
          "data-[state=checked]:translate-x-[16px] data-[state=unchecked]:translate-x-[2px]"
        )}
      />
    </SwitchPrimitive.Root>
  )
}

export { Switch }
