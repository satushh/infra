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

loki.source.file "beacon_logs" {
  targets = [
    {
      __path__     = "/var/log/beacon/beacon-chain.log",
      service_name = "beacon",
    },
  ]
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

loki.write "default" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
