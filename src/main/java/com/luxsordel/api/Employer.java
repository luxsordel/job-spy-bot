package com.luxsordel.api;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

@JsonIgnoreProperties(ignoreUnknown = true)

public record Employer(String name, 
        @JsonProperty("alternate_url") String alternate_url) {}
