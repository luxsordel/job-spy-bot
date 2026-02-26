package com.luxsordel.api;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

@JsonIgnoreProperties(ignoreUnknown = true)

public record Employer(String name, 
        @JsonProperty("alternate_url") String alternate_url) {}
