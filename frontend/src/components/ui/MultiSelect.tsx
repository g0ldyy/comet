import * as Popover from "@radix-ui/react-popover";
import { Check, ChevronDown, Search, X } from "lucide-react";
import { useState } from "react";

export interface MultiSelectOption {
  label: string;
  value: string;
}

interface MultiSelectProps {
  className?: string;
  emptyLabel: string;
  label: string;
  onChange: (selected: string[]) => void;
  options: readonly MultiSelectOption[];
  removeLabel: (label: string) => string;
  searchLabel: string;
  searchable?: boolean;
  selected: string[];
}

export function MultiSelect({
  className = "",
  emptyLabel,
  label,
  onChange,
  options,
  removeLabel,
  searchLabel,
  searchable = true,
  selected,
}: MultiSelectProps) {
  const [query, setQuery] = useState("");
  const selectedValues = new Set(selected);
  const selectedOptions = options.filter((option) => selectedValues.has(option.value));
  const visibleOptions = options.filter((option) =>
    option.label.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  );
  const toggleOption = (value: string) => {
    onChange(
      selectedValues.has(value)
        ? selected.filter((selectedValue) => selectedValue !== value)
        : [...selected, value],
    );
  };

  return (
    <div className={`field multi-select-field ${className}`.trim()}>
      <span className="field__label">{label}</span>
      <Popover.Root onOpenChange={(open) => !open && setQuery("")}>
        <div className="multi-select__trigger">
          {selectedOptions.length > 0 ? (
            <span className="multi-select__badges">
              {selectedOptions.map((option) => (
                <button
                  aria-label={removeLabel(option.label)}
                  className="multi-select__badge"
                  key={option.value}
                  onClick={() => toggleOption(option.value)}
                  type="button"
                >
                  <span>{option.label}</span>
                  <X aria-hidden="true" size={12} strokeWidth={1.6} />
                </button>
              ))}
            </span>
          ) : (
            <span className="multi-select__empty">{emptyLabel}</span>
          )}
          <Popover.Trigger aria-label={label} className="multi-select__open" type="button">
            <ChevronDown aria-hidden="true" className="select-trigger__icon" size={16} />
          </Popover.Trigger>
        </div>
        <Popover.Portal>
          <Popover.Content
            align="start"
            className="multi-select__content"
            collisionPadding={8}
            sideOffset={6}
          >
            {searchable ? (
              <div className="multi-select__search">
                <Search aria-hidden="true" size={15} />
                <input
                  aria-label={searchLabel}
                  autoComplete="off"
                  onChange={(event) => setQuery(event.target.value)}
                  value={query}
                />
              </div>
            ) : null}
            <div className="multi-select__options">
              {visibleOptions.map((option) => {
                const checked = selectedValues.has(option.value);
                return (
                  <button
                    aria-pressed={checked}
                    className="multi-select__option"
                    key={option.value}
                    onClick={() => toggleOption(option.value)}
                    type="button"
                  >
                    {checked ? (
                      <Check
                        aria-hidden="true"
                        className="multi-select__check"
                        size={15}
                        strokeWidth={1.6}
                      />
                    ) : (
                      <span aria-hidden="true" className="multi-select__check-placeholder" />
                    )}
                    <span>{option.label}</span>
                  </button>
                );
              })}
            </div>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </div>
  );
}
