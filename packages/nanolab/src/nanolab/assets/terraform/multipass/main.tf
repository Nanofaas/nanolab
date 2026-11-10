terraform {
  required_providers {
    multipass = {
      source  = "registry.opentofu.org/nanofaas/multipass"
      version = "= 0.1.0"
    }
  }
}

variable "ssh_host" {
  type    = string
  default = null
}
variable "ssh_user" {
  type    = string
  default = null
}
variable "multipass_path" {
  type    = string
  default = "multipass"
}
variable "ssh_public_key" { type = string }
variable "nodes" {
  type = map(object({ name = string, cpus = number, memory = string, disk = string }))
}

provider "multipass" {
  ssh_host       = var.ssh_host
  ssh_user       = var.ssh_user
  multipass_path = var.multipass_path
}

resource "multipass_instance" "node" {
  for_each            = var.nodes
  name                = each.value.name
  image               = "24.04"
  cpus                = each.value.cpus
  memory              = each.value.memory
  disk                = each.value.disk
  cloud_init          = yamlencode({ ssh_authorized_keys = [var.ssh_public_key] })
  wait_for_cloud_init = true
  resize_policy       = "replace"
}

output "nodes" {
  value = { for id, vm in multipass_instance.node : id => { name = vm.name, ipv4 = slice(vm.ipv4, 0, 1) } }
}
