pyroscope.write "default" {
  endpoint {
    url = "http://pyroscope:4040"
  }
}

pyroscope.scrape "beacon" {
  targets = [{"__address__" = "beacon:6060", "service_name" = "beacon"}]
  forward_to = [pyroscope.write.default.receiver]

  profiling_config {
    profile.process_cpu { enabled = true }
    profile.memory      { enabled = true }
    profile.goroutine   { enabled = true }
    profile.mutex       { enabled = true }
    profile.block       { enabled = true }
    profile.fgprof      { enabled = false }
  }
}

local.file_match "beacon" {
  path_targets = [
    {
      __path__     = "/var/log/beacon/*.log",
      service_name = "beacon",
    },
  ]
}

loki.source.file "beacon_logs" {
  targets    = local.file_match.beacon.targets
  forward_to = [loki.process.extract_level.receiver]
}

loki.process "extract_level" {
  stage.logfmt {
    mapping = {
      "level"   = "",
      "package" = "",
    }
  }

  stage.labels {
    values = {
      "level"   = "",
      "package" = "",
    }
  }

  stage.output {
    source = "output"
  }

  forward_to = [loki.write.default.receiver]
}

local.file_match "geth" {
  path_targets = [
    {
      __path__     = "/var/log/geth/*.log",
      service_name = "geth",
    },
  ]
}

loki.source.file "geth_logs" {
  targets    = local.file_match.geth.targets
  forward_to = [loki.write.default.receiver]
}

loki.write "default" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
