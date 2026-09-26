// Repository-scoped libdnf5 hooks. No downloads, RPM database or cached verdicts.
#include <libdnf5/base/base.hpp>
#include <libdnf5/base/transaction.hpp>
#include <libdnf5/base/transaction_package.hpp>
#include <libdnf5/plugin/iplugin.hpp>
#include <libdnf5/repo/repo_query.hpp>
#include <libdnf5/transaction/transaction_item_action.hpp>

#include <cerrno>
#include <chrono>
#include <csignal>
#include <filesystem>
#include <set>
#include <spawn.h>
#include <stdexcept>
#include <string>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {
constexpr const char * name = "oscmix-repository";
constexpr libdnf5::PluginAPIVersion api{2, 0};
constexpr libdnf5::plugin::Version version{0, 8, 0};
constexpr const char * attributes[]{nullptr};
constexpr const char * key = "file:///usr/share/keyrings/oscmix-desk-repository.asc";
std::exception_ptr last_exception;

bool project(const libdnf5::repo::Repo & repo) {
    const auto & keys = repo.get_config().get_gpgkey_option().get_value();
    for (const auto & value : keys) {
        if (value == key) {
            if (keys.size() != 1) {
                throw std::runtime_error("oscmix-desk repository requires its single scoped trust anchor");
            }
            return true;
        }
    }
    return false;
}

void verify(libdnf5::repo::Repo & repo) {
    namespace fs = std::filesystem;
    const auto & config = repo.get_config();
    if (!config.get_repo_gpgcheck_option().get_value() || !config.get_pkg_gpgcheck_option().get_value()) {
        throw std::runtime_error("oscmix-desk requires native metadata and package verification");
    }
    // This is this Repo object's cache, not a glob over historical repo IDs.
    // Its loaded primary metadata must belong to this very master index.
    const fs::path directory = fs::path(repo.get_cachedir()) / "repodata";
    const fs::path primary = repo.get_metadata_path("primary");
    if (primary.empty() || primary.parent_path() != directory) {
        throw std::runtime_error("oscmix-desk cannot identify the loaded repository metadata");
    }
    std::vector<std::string> arguments{
        "/usr/bin/python3", "-I", "/usr/lib/oscmix-desk-repository/verify.py",
        "--signature", (directory / "repomd.xml.asc").string(),
        "--source", (directory / "repomd.xml").string()};
    std::vector<char *> argv;
    for (auto & argument : arguments) argv.push_back(argument.data());
    argv.push_back(nullptr);
    char path[] = "PATH=/usr/bin:/bin:/usr/sbin:/sbin";
    char locale[] = "LC_ALL=C";
    char * environment[]{path, locale, nullptr};
    pid_t child;
    if (posix_spawn(&child, argv[0], nullptr, nullptr, argv.data(), environment)) {
        throw std::runtime_error("oscmix-desk cannot start repository verification");
    }
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(45);
    int status = 0;
    while (true) {
        const auto waited = waitpid(child, &status, WNOHANG);
        if (waited == child) break;
        if (waited < 0 && errno != EINTR) {
            throw std::runtime_error("oscmix-desk lost repository verification process");
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            kill(child, SIGKILL);
            while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
            throw std::runtime_error("oscmix-desk repository verification timed out");
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        throw std::runtime_error("oscmix-desk repository metadata lacks a current, unrevoked signature");
    }
}

class Repository final : public libdnf5::plugin::IPlugin {
public:
    explicit Repository(libdnf5::plugin::IPluginData & data) : IPlugin(data) {}
    libdnf5::PluginAPIVersion get_api_version() const noexcept override { return api; }
    const char * get_name() const noexcept override { return name; }
    libdnf5::plugin::Version get_version() const noexcept override { return version; }
    const char * const * get_attributes() const noexcept override { return attributes; }
    const char * get_attribute(const char *) const noexcept override { return nullptr; }

    void repos_loaded() override {
        libdnf5::repo::RepoQuery repositories(get_base());
        repositories.filter_enabled(true);
        repositories.filter_type(libdnf5::repo::Repo::Type::AVAILABLE);
        for (const auto & repo : repositories) {
            if (project(*repo) && !repo->get_metadata_path("primary").empty()) verify(*repo);
        }
    }

    void pre_transaction(const libdnf5::base::Transaction & transaction) override {
        std::set<std::string> checked;
        for (const auto & item : transaction.get_transaction_packages()) {
            if (!libdnf5::transaction::transaction_item_action_is_inbound(item.get_action())) continue;
            const auto repo = item.get_package().get_repo();
            if (project(*repo) && checked.insert(repo->get_id()).second) verify(*repo);
        }
    }
};
}  // namespace

libdnf5::PluginAPIVersion libdnf_plugin_get_api_version() { return api; }
const char * libdnf_plugin_get_name() { return name; }
libdnf5::plugin::Version libdnf_plugin_get_version() { return version; }
libdnf5::plugin::IPlugin * libdnf_plugin_new_instance(
    libdnf5::LibraryVersion, libdnf5::plugin::IPluginData & data, libdnf5::ConfigParser &) try {
    return new Repository(data);
} catch (...) {
    last_exception = std::current_exception();
    return nullptr;
}
void libdnf_plugin_delete_instance(libdnf5::plugin::IPlugin * instance) { delete instance; }
std::exception_ptr * libdnf_plugin_get_last_exception() { return &last_exception; }
