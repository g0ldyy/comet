interface SkeletonProps {
  label: string;
  lines?: number;
}

const skeletonLines = ["first", "second", "third", "fourth", "fifth"];

export function Skeleton({ label, lines = 3 }: SkeletonProps) {
  return (
    <div aria-busy="true" className="skeleton" role="status">
      <span className="visually-hidden">{label}</span>
      {skeletonLines.slice(0, lines).map((line) => (
        <span aria-hidden="true" key={line} />
      ))}
    </div>
  );
}
