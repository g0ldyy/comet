import { useQuery } from "@tanstack/react-query";
import { queryClient } from "../../api/query-client";
import { getCurrentMetrics } from "./api";
import { appendMetricFrame, type MetricFrame } from "./model";

const frameHistoryKey = ["admin", "metrics", "frames"] as const;

export function useLiveMetrics() {
  const history = useQuery({
    initialData: [] as MetricFrame[],
    queryFn: () => Promise.resolve([] as MetricFrame[]),
    queryKey: frameHistoryKey,
    staleTime: Number.POSITIVE_INFINITY,
  });
  const query = useQuery({
    queryFn: async () => {
      const snapshot = await getCurrentMetrics();
      queryClient.setQueryData<MetricFrame[]>(frameHistoryKey, (frames) =>
        appendMetricFrame(frames ?? [], snapshot),
      );
      return snapshot;
    },
    queryKey: ["admin", "metrics", "current"],
    refetchInterval: 5_000,
    staleTime: 1_000,
  });

  return { ...query, frames: history.data };
}
