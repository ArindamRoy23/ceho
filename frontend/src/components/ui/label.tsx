import { type ComponentProps } from "react";
import { cn } from "@/lib/utils.ts";

export function Label({ className, ...props }: ComponentProps<"label">) {
  return <label className={cn("text-sm font-medium leading-none", className)} {...props} />;
}
